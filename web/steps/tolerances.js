// Tolerances & fits (T-570, plan §15.2, §16.8), opened from Done's card. Click a round hole or peg on the part (the
// list does the same): a see-through mating part slides in with a clearance ring at the mouth, red when it will
// jam on this printer, amber when tight or loose, green when it fits, with the gap it gets ("0.18 mm gap, measured
// on your P1S" or "typical PLA"). Choose Press / Snug / Loose and the mating size, or the Screw / insert tab (M2–M6
// guessed from the hole, screw into plastic, heat-set insert, through bolt, nut pocket). "Apply to all N the same" is
// on by default. Adding it goes into Problems' pending batch, the same confirm path as every other change (§13):
// resized geometry, or hole compensation when only slicer settings may change (it affects every hole, so all holes are
// tinted). "What prints" overlays the size each hole and peg really comes out at, ×5 or ×10.
// Two more modes (T-573, steps/tolgaps.js): Flat gaps with a feeler gauge, and the Mating part heat map.
import * as THREE from "three";
import { h } from "../ui/dom.js";
import { Button } from "../ui/button.js";
import { SegmentedControl } from "../ui/choice.js";
import { NumberField } from "../ui/numberfield.js";
import { ToggleChip } from "../ui/chip.js";
import { openModal, showToast } from "../ui/overlay.js";
import { registerStep } from "../shell.js";
import { Cancelled } from "../engine.js";
import { t } from "../i18n.js";
import { optimizeArgs } from "./args.js";
import { simProfile } from "./simulate.js";
import { clearMate, clearPrinted, showMate, showPrinted } from "../viewport/tolerances3d.js";
import { gapFaces, hideGaps, hideMate, pickGapFace, renderGaps, renderMate } from "./tolgaps.js";

const FITS = ["press", "snug", "loose"];
const SIZES = ["M2", "M2.5", "M3", "M4", "M5", "M6"];
const USES = ["screw", "insert", "bolt", "nut_top"];
const f32 = (b) => new Float32Array(new Uint8Array(b).slice().buffer);

let data = null; // {key, features, tolerance}
let selected = null; // region
let form = null; // {mode: fit | fastener, fit, partner, size, use, all}
let answer = null; // set_fit / set_fastener for the selected feature
let printed = 0; // 0 | 5 | 10
let busy = null;
let error = null;
let wired = false;
const designOk = { value: false }; // "This changes your model" was confirmed once (shared with tolgaps.js)
let mode = "round"; // round | gaps | mate

const upOf = (s) => s.option?.up ?? [0, 0, 1];
const profileOf = (s) => (s.option ? simProfile(s, s.option) : optimizeArgs(s).profile);
const featureOf = (region) => data?.features.find((f) => f.region === region) ?? null;

/** The mating part's usual size for a feature: the nearest half millimetre (a Ø2.8 hole is for a Ø3 pin). */
export const nominal = (d) => Math.round(d * 2) / 2;
/** The screw a hole is for, from its diameter (clearance or tapping size). */
export function guessScrew(d) {
  const sizes = [[2, 2], [2.5, 2.5], [3, 3], [4, 4], [5, 5], [6, 6]];
  const best = sizes.reduce((a, b) => (Math.abs(b[1] - d) < Math.abs(a[1] - d) ? b : a));
  return `M${best[0]}`;
}

/**
 * What happens with a mating part of ``partner`` mm on feature ``f`` as it prints (``printed_mm``), for the gaps in
 * use (tolerance().gaps): {gap (mm, diametral), verdict: jam | tight | fits | loose}. A hole's gap is the printed hole
 * minus the pin; a peg's is the sleeve minus the printed peg.
 */
export function clearance(f, partner, fit, gaps) {
  const gap = f.kind === "hole" ? f.printed_mm - partner : partner - f.printed_mm;
  const want = gaps[fit] ?? gaps.snug;
  const verdict = gap <= 0 ? "jam" : gap < want * 0.6 ? "tight" : gap > Math.max(want * 1.8, gaps.loose * 1.2) ? "loose" : "fits";
  return { gap: Math.round(gap * 100) / 100, verdict };
}

async function load(store, services) {
  const s = store.get();
  const key = JSON.stringify([store.deps("model", "profile"), s.option?.up, s.option?.material]);
  if (data?.key === key || busy) return;
  busy = t("tol.loading");
  services.rerender();
  try {
    const r = await services.engine.call("round_features", { up: upOf(s), profile: profileOf(s) });
    data = { key, ...r };
    if (selected != null && !featureOf(selected)) selected = null;
  } catch (e) {
    if (!(e instanceof Cancelled)) error = e.message;
  } finally {
    busy = null;
    services.rerender();
  }
}

function select(region, store, services) {
  const f = featureOf(region);
  if (!f) {
    showToast({ message: t("tol.notRound") });
    return;
  }
  selected = region;
  form = {
    mode: f.fastener ? "fastener" : "fit",
    fit: FITS.includes(f.fit?.fit) ? f.fit.fit : "snug",
    partner: f.fit?.partner_mm ?? nominal(f.diameter),
    size: f.fastener?.size ?? guessScrew(f.diameter),
    use: f.fastener?.use ?? "screw",
    all: true,
  };
  answer = null;
  services.rerender();
  ask(store, services);
}

/** Ask the engine what to model for the current form (it also remembers it for design()). */
async function ask(store, services) {
  const s = store.get();
  const f = featureOf(selected);
  if (!f) return;
  try {
    answer = form.mode === "fit" || f.kind !== "hole"
      ? await services.engine.call("set_fit", { region: f.region, up: upOf(s), partner_mm: form.partner, fit: form.fit, profile: profileOf(s) })
      : await services.engine.call("set_fastener", { region: f.region, up: upOf(s), size: form.size, use: form.use, profile: profileOf(s) });
    error = null;
  } catch (e) {
    if (!(e instanceof Cancelled)) error = e.message;
  }
  services.rerender();
}

/** Put the change into Problems' pending batch: resized geometry, or hole compensation when only settings may change. */
async function add(store, services, asSetting) {
  const s = store.get();
  const f = featureOf(selected);
  const regions = form.all ? f.same : [f.region];
  busy = t("tol.adding");
  services.rerender();
  try {
    const up = upOf(s), profile = profileOf(s);
    for (const r of regions) {
      if (form.mode === "fit" || f.kind !== "hole") await services.engine.call("set_fit", { region: r, up, partner_mm: form.partner, fit: form.fit, profile });
      else await services.engine.call("set_fastener", { region: r, up, size: form.size, use: form.use, profile });
    }
    const label = regions.length > 1 ? t("tol.titleAll", { title: answerTitle(f), count: regions.length }) : answerTitle(f);
    let item;
    if (asSetting) {
      const comp = Math.max(0, Math.round(((answer.suggested_mm - answer.modelled_mm) / 2) * 100) / 100);
      item = { id: `tol:comp`, kind: "setting", findingId: null, title: t("tol.compTitle", { mm: comp }),
        changes: [{ key: "xy_hole_compensation", from: s.option?.settings?.xy_hole_compensation ?? null, to: String(comp) }] };
    } else {
      const design = await services.engine.call("design", { up, profile });
      const mine = (design.edits ?? []).filter((e) => e.source === "request" && regions.includes(Number(e.id.match(/-(\d+)$/)?.[1])))
        .map((e) => ({ ...e, mesh: f32(e.mesh) }));
      if (!mine.length) throw new Error(t("tol.noEdit"));
      item = { id: `tol:${regions.join(",")}`, kind: "design", findingId: null, title: label, editIds: mine.map((e) => e.id), edits: mine };
    }
    store.set({ pending: [...store.get().pending.filter((p) => p.id !== item.id), item] });
    showToast({ message: t("tol.added"), action: { label: t("tol.review"), onClick: () => services.go?.("problems") } });
  } catch (e) {
    if (!(e instanceof Cancelled)) error = e.message;
  } finally {
    busy = null;
    services.rerender();
  }
}

function answerTitle(f) {
  if (form.mode === "fastener" && f.kind === "hole") return t("tol.fastenerTitle", { size: form.size, use: t(`tol.use.${form.use}`), d: f.diameter });
  return t(`tol.fitTitle.${f.kind}`, { fit: t(`tol.fit.${form.fit}`), partner: form.partner, d: f.diameter, to: answer?.suggested_mm ?? "…" });
}

function wire(store, services) {
  if (wired) return;
  wired = true;
  services.picker.on("pick", (hit) => {
    if (store.get().step !== "tolerances" || hit.region == null) return;
    if (mode === "gaps") pickGapFace(hit, extraCtx(store, services));
    else if (mode === "round") select(hit.region, store, services);
  });
}

const extraCtx = (store, services, more = {}) => ({ store, services, upOf, profileOf, designOk, tolerance: data?.tolerance, ...more });

/** Leave one mode's 3D bits behind when switching (or leaving the step). */
function clearModes(services) {
  clearMate(services.stage);
  clearPrinted(services.stage);
  hideGaps(services);
  hideMate(services);
}

/** Tint the round features; the selected group stronger; all holes when hole compensation is the way. In Flat gaps,
 * the faces picked so far. */
function tint(store, services) {
  const picker = services.picker;
  if (!picker?.data || !data || !services.stage.part) return;
  const accent = new THREE.Color(getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#2563EB");
  if (mode !== "round") {
    const picked = new Set(mode === "gaps" ? gapFaces(store) : []);
    picker.setRegionColors((r) => (picked.has(r) ? accent : null));
    return;
  }
  const soft = services.stage.part.base.clone().lerp(accent, 0.35);
  const sel = new Set(form?.all ? featureOf(selected)?.same ?? [] : selected != null ? [selected] : []);
  const round = new Set(data.features.map((f) => f.region));
  picker.setRegionColors((r) => (sel.has(r) ? accent : round.has(r) ? soft : null));
}

function groups() {
  const seen = new Set();
  return data.features.filter((f) => {
    const key = f.same.join(",");
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

registerStep("tolerances", {
  render({ body, stage, store, go, services }) {
    services.go = go;
    wire(store, services);
    const s = store.get();
    load(store, services);
    if (busy) body.append(h("div", { class: "sim-running", role: "status" }, h("span", { class: "spinner", "aria-hidden": "true" }), h("span", {}, busy)));
    if (error) body.append(h("p", { class: "drop-error", role: "alert" }, error));
    if (!data) return;
    body.append(h("div", { class: "tol-modes" }, SegmentedControl({ label: t("tol.modes"), size: "small", value: mode,
      options: ["round", "gaps", "mate"].map((m) => ({ value: m, label: t(`tol.modes.${m}`) })),
      onChange: (v) => { clearModes(services); mode = v; services.rerender(); } })));
    tint(store, services);
    const tol = data.tolerance;
    if (mode === "gaps") { renderGaps(extraCtx(store, services, { body })); return; }
    if (mode === "mate") { renderMate(extraCtx(store, services, { body, stage })); return; }
    body.append(h("p", { class: "ui-hint" }, data.features.length ? t("tol.pick") : t("tol.none")));

    // The list: one row per size, the same as clicking one on the part.
    const list = h("div", { class: "tol-list" }, groups().map((f) => h("button", { type: "button", class: ["tol-row", f.same.includes(selected) && "is-selected"],
      onclick: () => select(f.region, store, services) },
    h("b", {}, t(`tol.row.${f.kind}`, { count: f.same.length, d: f.diameter })),
    h("span", { class: "ui-hint" }, f.fastener ? t("tol.setFastener", { size: f.fastener.size }) : f.fit ? t("tol.setFit", { fit: t(`tol.fit.${f.fit.fit}`), partner: f.fit.partner_mm }) : t("tol.notSet")))));
    body.append(list);

    const f = featureOf(selected);
    if (f && form) {
      const hole = f.kind === "hole";
      const card = h("div", { class: "tol-card" }, h("b", {}, t(`tol.selected.${f.kind}`, { d: f.diameter, count: f.same.length })));
      if (hole) {
        card.append(SegmentedControl({ label: t("tol.mode"), size: "small", value: form.mode,
          options: [{ value: "fit", label: t("tol.mode.fit") }, { value: "fastener", label: t("tol.mode.fastener") }],
          onChange: (v) => { form.mode = v; ask(store, services); } }));
      }
      if (form.mode === "fit" || !hole) {
        card.append(SegmentedControl({ label: t("tol.fitLabel"), size: "small", value: form.fit, options: FITS.map((x) => ({ value: x, label: t(`tol.fit.${x}`) })),
          onChange: (v) => { form.fit = v; ask(store, services); } }),
        NumberField({ label: t(hole ? "tol.partner.hole" : "tol.partner.peg"), value: form.partner, units: [{ value: "mm", label: "mm" }], min: 0.5, max: 200,
          onChange: (v) => { if (v != null && v !== form.partner) { form.partner = v; ask(store, services); } } }));
        const c = clearance(f, form.partner, form.fit, tol.gaps);
        const where = f.printed_from === "measured" ? t("tol.measured", { printer: s.catalog?.printers?.[s.profile?.printer]?.name ?? "" })
          : t("tol.typical", { material: s.catalog?.materials?.[s.option?.material ?? s.profile?.material]?.name ?? "" });
        card.append(h("p", { class: `tol-verdict is-${c.verdict}` }, h("b", {}, t(`tol.verdict.${c.verdict}`)), " ", c.gap < 0 ? t("tol.overlap", { mm: (-c.gap).toFixed(2), where }) : t("tol.gap", { gap: c.gap.toFixed(2), where })));
        showMate(services.stage, f, form.partner, c.verdict);
      } else {
        card.append(SegmentedControl({ label: t("tol.size"), size: "small", value: form.size, options: SIZES.map((x) => ({ value: x, label: x })),
          onChange: (v) => { form.size = v; ask(store, services); } }),
        SegmentedControl({ label: t("tol.use"), size: "small", value: form.use, options: USES.map((x) => ({ value: x, label: t(`tol.use.${x}`) })),
          onChange: (v) => { form.use = v; ask(store, services); } }));
        const ok = answer?.target_mm != null && Math.abs(answer.target_mm - f.diameter) <= 0.1;
        card.append(h("p", { class: `tol-verdict is-${ok ? "fits" : "tight"}` }, answer?.target_mm != null
          ? t(ok ? "tol.fastenerOk" : "tol.fastenerChange", { to: answer.target_mm, d: f.diameter }) : ""));
        showMate(services.stage, f, Number(form.size.slice(1)), ok ? "fits" : "tight");
      }
      if (answer?.explanation) card.append(h("p", { class: "ui-hint" }, answer.explanation));
      if (f.same.length > 1) card.append(ToggleChip({ label: t("tol.all", { count: f.same.length, d: f.diameter }), pressed: form.all, onChange: (v) => { form.all = v; services.rerender(); } }));
      const slicerOnly = s.change === "slicer";
      const grow = answer?.suggested_mm != null && answer.suggested_mm > answer.modelled_mm && hole && (form.mode === "fit");
      const actions = h("div", { class: "row" });
      if (slicerOnly && grow) actions.append(Button({ label: t("tol.addComp"), variant: "primary", size: "small", disabled: Boolean(busy), onClick: () => add(store, services, true) }));
      actions.append(Button({ label: slicerOnly ? t("tol.addModel") : t("tol.add"), variant: slicerOnly && grow ? "secondary" : "primary", size: "small", disabled: Boolean(busy) || !answer,
        onClick: () => {
          if (!slicerOnly || designOk.value) return add(store, services, false);
          openModal({ title: t("problems.modelTitle"), body: h("p", {}, t("problems.modelBody")), actions: [
            { label: t("ui.cancel"), variant: "ghost" }, { label: t("ui.add"), variant: "primary", onClick: () => { designOk.value = true; add(store, services, false); } }] });
        } }));
      if (slicerOnly && grow) card.append(h("p", { class: "ui-hint" }, t("tol.compNote")));
      card.append(actions);
      body.append(card);
    } else {
      clearMate(services.stage);
    }

    body.append(h("p", { class: "ui-hint" }, tol.explanation));
    const pending = s.pending.filter((p) => String(p.id).startsWith("tol:")).length;
    if (pending) body.append(Button({ label: t("tol.review"), variant: "secondary", onClick: () => go("problems") }));

    // What prints, on the stage.
    const bar = h("div", { class: "sim-views" }, h("span", { class: "tol-printed-label" }, t("tol.printed")), SegmentedControl({ label: t("tol.printed"), size: "small", value: String(printed),
      options: [{ value: "0", label: t("tol.printed.off") }, { value: "5", label: "×5" }, { value: "10", label: "×10" }],
      onChange: (v) => { printed = Number(v); services.rerender(); } }));
    stage.append(bar);
    if (printed) {
      showPrinted(services.stage, data.features, printed);
      stage.append(h("div", { class: "sim-legend" }, h("b", {}, t("tol.printedLegend", { factor: printed })), h("span", {}, t("tol.printedNote", { factor: printed }))));
    } else clearPrinted(services.stage);
  },
  leave({ services }) {
    clearModes(services);
    services.picker?.setRegionColors(() => null);
    selected = null;
    form = null;
    answer = null;
  },
});
