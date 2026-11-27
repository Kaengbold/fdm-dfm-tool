// Tolerances & fits, the later extras (T-573, plan §15.2 ideas 3 and 4): flat gaps with a feeler gauge, and the
// mating part heat map. Flat gaps: click two flat faces facing each other (a slot) or away (a tab); the engine
// measures them (api.set_gap) and, with what goes in and the fit, says the size to model; a blade (or a pair of slot
// walls over a tab) slides through and stops red at the mouth where it jams. The change goes into Problems' pending
// batch like a hole's (the design edit gap-a-b). Mating part: load the other part from the same assembly
// (api.clearance_map): the gap is painted on this part, red where they touch, with the tightest spot marked.
import { h } from "../ui/dom.js";
import { Button } from "../ui/button.js";
import { SegmentedControl } from "../ui/choice.js";
import { NumberField } from "../ui/numberfield.js";
import { ToggleChip } from "../ui/chip.js";
import { openModal, showToast } from "../ui/overlay.js";
import { Cancelled } from "../engine.js";
import { t } from "../i18n.js";
import { clearFeeler, clearTight, showFeeler, showTight } from "../viewport/tolerances3d.js";

const GAP_FITS = ["press", "snug", "sliding", "loose"];
const f32 = (b) => new Float32Array(new Uint8Array(b).slice().buffer);

const gap = { key: null, faces: [], answer: null, partner: null, fit: "sliding", busy: null, error: null, shown: null };
const mate = { key: null, result: null, name: null, show: true, busy: null, error: null };

const keyOf = (store) => {
  const s = store.get();
  return JSON.stringify([store.deps("model", "profile"), s.option?.up, s.option?.material]);
};

function syncGap(store, services) {
  const key = keyOf(store);
  if (gap.key === key) return;
  gap.key = key;
  gap.faces = [];
  gap.answer = null;
  gap.partner = null;
  gap.error = null;
  gap.shown = null;
  if (services) clearFeeler(services.stage);
}

function syncMate(store, services) {
  const key = keyOf(store);
  if (mate.key === key) return;
  mate.key = key;
  mate.result = null;
  mate.name = null;
  mate.error = null;
  if (services) hideMate(services);
}

/**
 * What a ``partner`` mm part does in a flat gap ``distance`` mm wide (a slot) or over a tab ``distance`` mm thick,
 * for the fit's total gap (``gaps[fit]``) and what warping closes (``warp``): {gap (mm), verdict: jam | tight | fits | loose}.
 */
export function feelerVerdict(kind, distance, partner, fit, gaps, warp = 0) {
  const g = (kind === "slot" ? distance - partner : partner - distance) - warp;
  const want = gaps[fit] ?? gaps.sliding ?? 0.3;
  const verdict = g <= 0 ? "jam" : g < want * 0.6 ? "tight" : g > Math.max(want * 1.8, (gaps.loose ?? want) * 1.2) ? "loose" : "fits";
  return { gap: Math.round(g * 100) / 100, verdict };
}

/** The colour of a map value: the gap (mm) as 0 (touching or overlapping, red) … 1 (``range`` or more apart, blue). */
export const gapShade = (mm, range) => Math.min(1, Math.max(0, mm / range));

async function measure(ctx) {
  const { store, services, upOf, profileOf } = ctx;
  const s = store.get();
  const key = keyOf(store);
  gap.key = key;
  const [a, b] = gap.faces;
  gap.busy = t("tol.gap.measuring");
  gap.error = null;
  services.rerender();
  try {
    const first = await services.engine.call("set_gap", { face_a: a, face_b: b, up: upOf(s), profile: profileOf(s) });
    if (gap.key !== key) return;
    if (gap.partner == null) gap.partner = first.kind === "slot" ? Math.max(0.5, Math.floor(first.distance_mm * 2) / 2 - 0.5) : Math.ceil(first.distance_mm * 2) / 2 + 0.5;
    const answer = await services.engine.call("set_gap", { face_a: a, face_b: b, up: upOf(s), profile: profileOf(s), partner_mm: gap.partner, fit: gap.fit });
    if (gap.key !== key) return;
    gap.answer = answer;
  } catch (e) {
    if (gap.key !== key) return;
    if (!(e instanceof Cancelled)) gap.error = e.message;
    gap.faces = [];
    gap.answer = null;
  } finally {
    gap.busy = null;
    services.rerender();
  }
}

/** A click in Flat gaps: the first face, then the second (then measure); a third click starts over. */
export function pickGapFace(hit, ctx) {
  syncGap(ctx.store, ctx.services);
  if (hit.region == null || gap.busy) return;
  const info = ctx.services.picker.data?.regions?.[hit.region];
  if (info && info.kind !== "flat") { showToast({ message: t("tol.gap.flatOnly") }); return; }
  if (gap.faces.length >= 2 || gap.faces.includes(hit.region)) { gap.faces = []; gap.answer = null; gap.partner = null; clearFeeler(ctx.services.stage); }
  gap.faces = [...gap.faces, hit.region];
  ctx.services.rerender();
  if (gap.faces.length === 2) measure(ctx);
}

export const gapFaces = (store) => (store && gap.key !== keyOf(store) ? [] : gap.faces);

async function addGap(ctx) {
  const { store, services, upOf, profileOf } = ctx;
  const s = store.get();
  const [a, b] = [...gap.faces].sort((x, y) => x - y);
  gap.busy = t("tol.adding");
  services.rerender();
  try {
    const design = await services.engine.call("design", { up: upOf(s), profile: profileOf(s) });
    const mine = (design.edits ?? []).filter((e) => e.id === `gap-${a}-${b}`).map((e) => ({ ...e, mesh: f32(e.mesh) }));
    if (!mine.length) throw new Error(t("tol.noEdit"));
    const item = { id: `tol:gap-${a}-${b}`, kind: "design", findingId: null, title: mine[0].title, editIds: mine.map((e) => e.id), edits: mine };
    store.set({ pending: [...store.get().pending.filter((p) => p.id !== item.id), item] });
    showToast({ message: t("tol.added"), action: { label: t("tol.review"), onClick: () => services.go?.("problems") } });
  } catch (e) {
    if (!(e instanceof Cancelled)) gap.error = e.message;
  } finally {
    gap.busy = null;
    services.rerender();
  }
}

const busyLine = (x) => [
  x.busy && h("div", { class: "sim-running", role: "status" }, h("span", { class: "spinner", "aria-hidden": "true" }), h("span", {}, x.busy)),
  x.error && h("p", { class: "drop-error", role: "alert" }, x.error)].filter(Boolean);

export function renderGaps(ctx) {
  const { body, store, services, tolerance, designOk } = ctx;
  syncGap(store, services);
  const s = store.get();
  body.append(h("p", { class: "ui-hint" }, t(gap.faces.length === 1 ? "tol.gap.pickSecond" : "tol.gap.pick")), ...busyLine(gap));
  const r = gap.answer;
  if (!r) { clearFeeler(services.stage); gap.shown = null; return; }
  const fits = SegmentedControl({ label: t("tol.fitLabel"), size: "small", value: gap.fit, options: GAP_FITS.map((x) => ({ value: x, label: t(`tol.gap.fit.${x}`) })),
    onChange: (v) => { gap.fit = v; measure(ctx); } });
  const partner = NumberField({ label: t(r.kind === "slot" ? "tol.gap.partner.slot" : "tol.gap.partner.tab"), value: gap.partner, units: [{ value: "mm", label: "mm" }], min: 0.2, max: 200,
    onCommit: (v) => { gap.partner = v; measure(ctx); } });
  const c = feelerVerdict(r.kind, r.distance_mm, gap.partner, gap.fit, tolerance.gaps, r.warp_mm ?? 0);
  const card = h("div", { class: "tol-card" },
    h("b", {}, t(`tol.gap.${r.kind}`, { mm: r.distance_mm.toFixed(2) })),
    fits, partner,
    h("p", { class: `tol-verdict is-${c.verdict}` }, h("b", {}, t(`tol.verdict.${c.verdict}`)), " ",
      c.gap <= 0 ? t("tol.gap.jams", { mm: (-c.gap).toFixed(2) }) : t("tol.gap.room", { mm: c.gap.toFixed(2) })),
    r.explanation ? h("p", { class: "ui-hint" }, r.explanation) : null);
  if (r.suggested_mm != null && Math.abs(r.suggested_mm - r.distance_mm) >= 0.02) {
    const slicerOnly = s.change === "slicer";
    card.append(h("div", { class: "row" }, Button({ label: t(slicerOnly ? "tol.addModel" : "tol.add"), variant: "primary", size: "small", disabled: Boolean(gap.busy),
      onClick: () => {
        if (!slicerOnly || designOk.value) return addGap(ctx);
        openModal({ title: t("problems.modelTitle"), body: h("p", {}, t("problems.modelBody")), actions: [
          { label: t("ui.cancel"), variant: "ghost" }, { label: t("ui.add"), variant: "primary", onClick: () => { designOk.value = true; addGap(ctx); } }] });
      } })));
  }
  body.append(card);
  const key = JSON.stringify([gap.faces, gap.partner, gap.fit, c.verdict]);
  if (gap.shown !== key || !services.stage.part?.group.getObjectByName("tol-feeler")) { // slides in once per change, not per redraw
    gap.shown = key;
    showFeeler(services.stage, r.plane, r.kind, r.distance_mm, gap.partner, c.verdict);
  }
}

async function loadMate(file, ctx) {
  const { store, services, profileOf } = ctx;
  if (!file) return;
  const key = keyOf(store);
  mate.key = key;
  mate.busy = t("tol.mate.measuring");
  mate.error = null;
  services.rerender();
  try {
    const bytes = new Uint8Array(await file.arrayBuffer());
    const r = await services.engine.call("clearance_map", { profile: profileOf(store.get()) }, bytes);
    if (mate.key !== key) return;
    mate.result = r;
    mate.name = file.name;
  } catch (e) {
    if (mate.key !== key) return;
    if (!(e instanceof Cancelled)) mate.error = e.message;
  } finally {
    mate.busy = null;
    services.rerender();
  }
}

export function renderMate(ctx) {
  const { body, stage, store, services } = ctx;
  syncMate(store, services);
  const input = h("input", { type: "file", accept: ".stl,.3mf,.obj", class: "visually-hidden", tabindex: "-1", "aria-hidden": "true",
    onchange: (e) => { loadMate(e.target.files[0], ctx); e.target.value = ""; } });
  body.append(h("p", { class: "ui-hint" }, t("tol.mate.intro")),
    Button({ label: mate.result ? t("tol.mate.other") : t("tol.mate.load"), variant: mate.result ? "secondary" : "primary", disabled: Boolean(mate.busy), onClick: () => input.click() }), input,
    ...busyLine(mate));
  const r = mate.result;
  if (!r) { hideMate(services); return; }
  body.append(h("div", { class: "tol-card" }, h("b", {}, mate.name),
    h("p", { class: `tol-verdict is-${{ overlap: "jam", "too tight": "jam", press: "tight", snug: "fits", sliding: "fits", loose: "loose", free: "loose" }[r.verdict] ?? "fits"}` },
      h("b", {}, t(`tol.mate.verdict.${r.verdict.replace(" ", "_")}`)), " ", r.gap_mm != null ? t("tol.mate.closest", { mm: r.gap_mm.toFixed(2) }) : ""),
    h("p", { class: "ui-hint" }, r.explanation),
    ToggleChip({ label: t("tol.mate.show"), pressed: mate.show, onChange: (v) => { mate.show = v; services.rerender(); } })));
  const gaps = f32(r.vertex_gap_mm);
  services.stage.showField({ triangles: f32(r.field_triangles), values: gaps.map((g) => 1 - gapShade(g, r.range_mm)), max: 1 });
  if (mate.show) services.stage.showGhosts([{ mesh: f32(r.mate_triangles), op: "add" }]);
  else services.stage.clearGhosts();
  showTight(services.stage, r.tight_at, Math.max(...(store.get().part?.size ?? [20])) * 0.02);
  stage.append(h("div", { class: "sim-legend" }, h("b", {}, t("tol.mate.legend")), h("span", {}, t("tol.mate.legendRange", { mm: r.range_mm }))));
}

/** Leaving the mode (or the step): the map, the mating part and the marks go. */
export function hideMate(services) {
  services.stage.hideField();
  services.stage.clearGhosts();
  clearTight(services.stage);
}

export function hideGaps(services) { clearFeeler(services.stage); gap.shown = null; }
