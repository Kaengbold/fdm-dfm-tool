// Simulation step (T-560, plan §12, §16.1): how the chosen option actually prints. As soon as the step opens, the
// browser OrcaSlicer (web/slicer-worker.js, T-525) slices the option in its own worker, turned as it prints
// (api.print_mesh) and with the option's settings. Run simulations then reads that G-code (api.load_gcode) so the
// print checks and the warping use its real layer times and spot heat; without it (slicer still loading, failed,
// too big, unknown printer) our own slicer stands in, and the panel says so. A G-code the user brings ("Have a sliced
// file?") works the same way. Results stay here first: a three-line status and an overlay switch on the part (Heat,
// Warping true size / exaggerated, Internal stress, Overhangs). Later simulations join the same button through
// registerSimulation: the heat model and the cooling plan (web/steps/heat.js, T-478) add their status line, a panel
// section and their own Heat view.
import { h } from "../ui/dom.js";
import { Button } from "../ui/button.js";
import { SegmentedControl } from "../ui/choice.js";
import { Disclosure } from "../ui/disclosure.js";
import { ProgressCard } from "../ui/progress.js";
import { registerStep } from "../shell.js";
import { Cancelled } from "../engine.js";
import { fieldColor } from "../viewport/stage.js";
import { t } from "../i18n.js";
import { markedFaces, optimizeArgs } from "./args.js";

export const DEPENDS = ["model", "option"];
const COOLING = /^(heat_|small_layers_cooling$)/;
const WARP_SCALE_MM = 1; // one colour scale for every material: ABS's bigger curl has to look bigger than PLA's
const f32 = (b) => new Float32Array(new Uint8Array(b).slice().buffer);

let slice = null; // {key, state: slicing | ready | fallback, percent, gcode, source: orca | file, reason, promise, abort}
let run = null; // {stage} while the simulations run
let lastError = null;
let overlay = "normal";
let warpSize = "true";
let shown = null; // what the 3D view shows now (key), so a re-render doesn't rebuild it
const extra = [];

/** A later simulation joins Run simulations. ``sim``: {name, stage (message key sim.stage.<stage> shown while it
 * runs), run({engine, up, profile, detailed, check, warp, state, gcode}) → its result (``status``: {label, value,
 * tone} for the status list) or nothing; optional panel({body, store, services, value, overlay}) adds a section
 * under the status, and showHeat({services, value}) → true draws the Heat view itself (clearHeat(services) takes it
 * away again)}. ``gcode``: the G-code bytes the run read, if any. */
export function registerSimulation(sim) { extra.push(sim); }
const extraValue = (result, sim) => result?.extra?.find((x) => x.name === sim.name)?.value;

/** The engine profile for the chosen option: the user's printer and use conditions with the option's material, nozzle
 * and layer height. */
export function simProfile(state, option) {
  return { ...optimizeArgs(state).profile, material: option.material, nozzle: String(option.nozzle), layer_height: option.layer_height };
}

/** The chosen setup in one line: "Flat · PETG · 0.4 mm · 3 walls · 15 % infill". */
export function setupLine(option, catalog) {
  return t("sim.setup", {
    orientation: option.orientation ?? t("sim.custom"),
    material: option.material_name ?? catalog?.materials?.[option.material]?.name ?? option.material,
    nozzle: option.nozzle, walls: t("sim.walls", { count: option.walls }), infill: Math.round((option.infill ?? 0) * 100),
  });
}

/** What the status lines and the footer say about a result. */
export function summarize(result) {
  const heatRes = result?.extra?.find((x) => x.name === "heat")?.value?.result;
  // When the 3D thermal model ran cleanly and found 0 hot spots, don't contradict it with the old 1-D cooling finding (T-618).
  const thermalClear = heatRes && (heatRes.before?.too_hot ?? 0) === 0;
  const findings = (result?.check?.findings ?? []).filter((f) => !(thermalClear && f.rule === "small_layers_cooling"));
  const cooling = findings.filter((f) => COOLING.test(f.rule) && f.severity !== "info");
  const issues = findings.filter((f) => !COOLING.test(f.rule) && f.severity !== "info");
  const verdict = result?.warp?.verdict ?? null;
  const warpBad = verdict === "likely to lift" || verdict === "might lift";
  return { cooling: cooling.length, issues: issues.length, verdict, warpBad, problems: cooling.length + issues.length + (warpBad ? 1 : 0) };
}

function startSlice(store, services) {
  const s = store.get();
  const o = s.option;
  const key = JSON.stringify(store.deps(...DEPENDS));
  if (!o || !s.part || (slice && slice.key === key)) return;
  slice?.abort?.abort();
  const abort = new AbortController();
  const mine = { key, state: "slicing", percent: 0, abort };
  slice = mine;
  mine.promise = (async () => {
    try {
      const mod = await import("../slicer-worker.js");
      const mesh = await services.engine.call("print_mesh", { up: o.up, profile: simProfile(s, o) });
      const { nozzle_diameter: _n, ...settings } = o.settings ?? {};
      const res = await mod.sliceInBrowser({ mesh: f32(mesh.triangles), name: s.part.name ?? "part.stl",
        printer: s.catalog?.printers?.[s.profile?.printer]?.name ?? s.profile?.printer, nozzle: String(o.nozzle),
        material: o.material, layerHeight: o.layer_height, settings },
      { signal: abort.signal, onProgress: (p) => { mine.percent = Math.round(p?.percent ?? 0); updateLine(); } });
      if (res.cancelled) return;
      if (res.ok) Object.assign(mine, { state: "ready", source: "orca", gcode: res.gcode, seconds: (res.timings?.wallMs ?? 0) / 1000 });
      else Object.assign(mine, { state: "fallback", reason: res.reason ?? "failed" });
    } catch (e) {
      Object.assign(mine, { state: "fallback", reason: e instanceof Cancelled ? "cancelled" : "failed" });
    } finally {
      if (slice === mine && !abort.signal.aborted) services.rerender();
    }
  })();
}

/** The chosen option's G-code (Done's Download G-code): the background slice when it is for this option (or the
 * user's own sliced file), else a new one. Resolves to {state: ready | fallback, gcode, source, reason}. */
export async function optionGcode(store, services) {
  startSlice(store, services);
  await slice?.promise;
  return slice;
}

/** The slicer line, updated in place while slicing (a full re-render per percent would be wasteful). */
function sliceText() {
  if (!slice) return t("sim.slice.waiting");
  if (slice.state === "slicing") return t("sim.slice.running", { percent: slice.percent });
  if (slice.state === "ready") return slice.source === "file" ? t("sim.slice.file", { name: slice.name }) : t("sim.slice.done", { seconds: slice.seconds.toFixed(1) });
  return t(`sim.slice.fallback.${["too_big", "no_profile", "unavailable"].includes(slice.reason) ? slice.reason : "failed"}`);
}
function updateLine() {
  const el = document.querySelector(".sim-slice__text");
  if (el) el.textContent = sliceText();
}

async function useFile(file, store, services) {
  slice?.abort?.abort();
  const gcode = new Uint8Array(await file.arrayBuffer());
  slice = { key: JSON.stringify(store.deps(...DEPENDS)), state: "ready", source: "file", name: file.name, gcode };
  services.rerender();
}

const simCardState = (r) => ({
  done: r.done,
  share: Math.min(1, (r.done + (r.sub ?? 0)) / Math.max(r.stages.length, 1)),
  elapsed: (performance.now() - r.started) / 1000,
});

export async function runSimulations(store, services) {
  const s = store.get();
  const o = s.option;
  if (!o) return false;
  const profile = simProfile(s, o);
  const engine = services.engine;
  const stages = ["slice", "gcode", "warp", "check", ...extra.map((x) => x.stage).filter(Boolean)];
  const setStage = (name) => {
    if (!run) return;
    run.stage = name;
    const idx = run.stages.indexOf(name);
    if (idx >= 0) run.done = idx;
    run.sub = 0;
    run.card?.set(simCardState(run));
  };
  lastError = null;
  const me = { stage: "slice", stages, done: 0, sub: 0, started: performance.now(), card: null, timer: null };
  run = me;
  me.timer = setInterval(() => run?.card?.set(simCardState(run)), 500);
  const prevProgress = services.onProgress;
  services.onProgress = (fraction) => {
    if (run !== me) return;
    me.sub = Math.max(0, Math.min(0.99, fraction ?? 0));
    me.card?.set(simCardState(me));
  };
  services.rerender();
  try {
    if (slice?.state === "slicing") await slice.promise;
    let gcode = null;
    let gcodeError = null;
    let data = null;
    if (slice?.state === "ready") {
      setStage("gcode");
      data = typeof slice.gcode === "string" ? new TextEncoder().encode(slice.gcode) : slice.gcode;
      try {
        gcode = await engine.call("load_gcode", { up: o.up, profile }, data);
      } catch (e) {
        if (e instanceof Cancelled) throw e;
        gcodeError = e.message; // not this part / orientation: our own slicer stands in
      }
    }
    const detailed = Boolean(gcode);
    setStage("warp");
    const warp = await engine.call("warping", { up: o.up, profile, detailed });
    setStage("check");
    const check = await engine.call("check", { up: o.up, profile, detailed, faces: markedFaces(s) });
    const more = [];
    for (const sim of extra) {
      setStage(sim.stage ?? run.stage);
      const value = await sim.run({ engine, up: o.up, profile, detailed, check, warp, state: s, gcode: detailed ? data : null });
      if (value) more.push({ name: sim.name, value, status: value.status });
    }
    store.setResult("simulate", { check, warp, gcode, gcodeError, detailed, source: detailed ? slice.source : "ours", extra: more },
      store.deps(...DEPENDS));
    return true;
  } catch (e) {
    if (!(e instanceof Cancelled)) lastError = e.message;
    return false;
  } finally {
    if (me.timer) clearInterval(me.timer);
    if (services.onProgress && run === me) services.onProgress = prevProgress;
    if (run === me) run = null;
    services.rerender();
  }
}

// ---------- the overlays on the part ----------

/** Faces steeper than the material allows, away from the bed: 0 (just over) … 1 (flat ceiling), or −1. */
export function overhangs(positions, up, limitDeg) {
  const n = positions.length / 9;
  const u = normalize(up);
  const out = new Float32Array(n).fill(-1);
  let low = Infinity;
  for (let i = 0; i < positions.length; i += 3) low = Math.min(low, positions[i] * u[0] + positions[i + 1] * u[1] + positions[i + 2] * u[2]);
  const sinLimit = Math.sin((limitDeg * Math.PI) / 180);
  for (let k = 0; k < n; k++) {
    const p = positions.subarray(k * 9, k * 9 + 9);
    const a = [p[3] - p[0], p[4] - p[1], p[5] - p[2]], b = [p[6] - p[0], p[7] - p[1], p[8] - p[2]];
    const nrm = normalize([a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]);
    const down = -(nrm[0] * u[0] + nrm[1] * u[1] + nrm[2] * u[2]); // sin of the angle past vertical
    const height = ((p[0] + p[3] + p[6]) * u[0] + (p[1] + p[4] + p[7]) * u[1] + (p[2] + p[5] + p[8]) * u[2]) / 3 - low;
    if (down > sinLimit && height > 0.3) out[k] = (down - sinLimit) / (1 - sinLimit || 1);
  }
  return out;
}
function normalize(v) {
  const l = Math.hypot(v[0], v[1], v[2]) || 1;
  return [v[0] / l, v[1] / l, v[2] / l];
}

function showOverlay(store, services, legend) {
  const stage = services.stage;
  if (!stage.part) return;
  const s = store.get();
  const result = store.isStale("simulate") ? null : store.result("simulate");
  const key = JSON.stringify([overlay, warpSize, Boolean(result), store.deps(...DEPENDS)]);
  const warp = result?.warp;
  if (overlay === "warp" || overlay === "stress") {
    if (!warp) return;
    const stress = overlay === "stress";
    const max = Math.max(stress ? warp.stress_max : Math.max(warp.max_mm, WARP_SCALE_MM), 1e-9);
    if (shown !== key) {
      stage.paint(() => null);
      stage.showField({ triangles: f32(warp.field_triangles), values: f32(stress ? warp.stress_value : warp.vertex_value), disp: f32(warp.vertex_disp), max });
      const big = (0.06 * stage.part.geo.boundingSphere.radius) / Math.max(warp.max_mm, 1e-9);
      stage.setDeformation(stress ? 0 : warpSize === "big" ? big : 1);
    }
    legend.append(h("b", {}, stress ? t("sim.legend.stress", { max: warp.stress_max.toFixed(1) }) : t("sim.legend.warp", { max: warp.max_mm.toFixed(2) })),
      h("span", {}, stress ? t("sim.legend.stressNote") : warpSize === "big" ? t("sim.legend.big") : t("sim.legend.true")));
  } else if (overlay === "heat" && result && extra.some((sim) => sim.showHeat?.({ services, value: extraValue(result, sim), legend }))) {
    stage.hideField(); // a later simulation draws its own heat view and fills the on-stage color legend (T-612)
    shown = null;
    return;
  } else if (shown !== key) {
    stage.hideField();
    if (overlay === "heat" && result?.check?.face_temp) {
      const heat = new Uint8Array(result.check.face_temp);
      stage.paint((i) => (heat[i] ? fieldColor(0.45 + 0.55 * (heat[i] / 255)) : null));
    } else if (overlay === "overhang") {
      const limit = s.catalog?.materials?.[s.option.material]?.max_overhang_deg ?? 50;
      const over = overhangs(stage.part.geo.attributes.position.array, s.option.up, limit);
      stage.paint((i) => (over[i] >= 0 ? fieldColor(0.6 + 0.4 * over[i]) : null));
    } else {
      stage.paint(() => null);
    }
  }
  if (overlay === "heat") {
    legend.append(h("b", {}, t("sim.legend.heat")), h("span", {}, t("sim.legend.heatNote")));
    if (!result?.detailed) legend.append(h("span", { class: "sim-legend__note" }, t("sim.legend.heatFallback")));
  }
  if (overlay === "overhang") {
    legend.append(h("b", {}, t("sim.legend.overhang")), h("span", {}, t("sim.legend.overhangNote")),
      h("span", { class: "sim-legend__note" }, t("sim.legend.overhangHint")));
  }
  shown = key;
}

function clearOverlay(services) {
  for (const sim of extra) sim.clearHeat?.(services);
  if (overlay === "overhang" && services.stage?.camera?.position?.z < 0) services.stage.view("iso");
  if (shown == null) return;
  services.stage.hideField();
  services.stage.paint(() => null);
  shown = null;
}

// ---------- the step ----------

registerStep("sim", {
  render({ body, stage, store, go, services }) {
    const s = store.get();
    const o = s.option;
    if (!o) {
      body.append(h("p", { class: "ui-hint" }, t("sim.noOption")), Button({ label: t("sim.toOptions"), onClick: () => go("options") }));
      return;
    }
    startSlice(store, services);
    const result = store.result("simulate");
    const fresh = result && !store.isStale("simulate");

    body.append(h("div", { class: "sim-setup" },
      h("span", {}, setupLine(o, s.catalog)),
      h("button", { type: "button", class: "ui-link", onclick: () => go("options") }, t("sim.change"))));
    if (o.warning === "below_target") body.append(h("p", { class: "analyse-stale" }, t("sim.belowTarget", { sf: o.safety, target: o.target })));

    const printer = s.catalog?.printers?.[s.profile?.printer];
    const material = s.catalog?.materials?.[o.material];
    const rows = [
      [t("sim.cond.enclosure"), t(`sim.enclosure.${printer?.enclosure ?? "none"}`), t("sim.cond.fromPrinter", { name: printer?.name ?? "—" })],
      material?.bed_temp_c != null ? [t("sim.cond.bed"), `${material.bed_temp_c} °C`, t("sim.cond.fromMaterial", { name: material.name })] : null,
      [t("sim.cond.room"), "20 °C", t("sim.cond.usual")],
    ].filter(Boolean);
    body.append(Disclosure({ label: t("sim.conditions"), summary: rows.map((r) => r[1]).join(" · "),
      content: h("dl", { class: "sim-cond" }, ...rows.flatMap(([k, v, src]) => [h("dt", {}, k), h("dd", {}, v, " ", h("small", {}, src))])) }));

    const file = h("input", { type: "file", accept: ".gcode,.gco,.g,.3mf,.bgcode", hidden: true, onchange: (e) => e.target.files[0] && useFile(e.target.files[0], store, services) });
    body.append(h("div", { class: "sim-slice", role: "status" },
      h("span", { class: "sim-slice__text" }, sliceText()),
      h("button", { type: "button", class: "ui-link", onclick: () => file.click() }, t("sim.haveFile")), file));

    if (run) {
      const card = ProgressCard({ title: t("sim.runningTitle"), stages: run.stages.map((k) => t(`sim.stage.${k}`)),
        onCancel: () => services.engine.cancel() });
      run.card = card;
      card.set(simCardState(run));
      stage.append(h("div", { class: "analyse-progress" }, card));
      body.append(Button({ label: t("sim.runningShort"), variant: "primary", disabled: true }));
    } else {
      body.append(Button({ label: fresh ? t("sim.again") : t("sim.run"), variant: fresh ? "secondary" : "primary", onClick: () => runSimulations(store, services) }));
    }
    if (lastError) body.append(h("p", { class: "drop-error", role: "alert" }, t("sim.failed", { reason: lastError })));
    if (result && !fresh) body.append(h("p", { class: "analyse-stale" }, t("sim.stale")));

    if (fresh) {
      const sum = summarize(result);
      const line = (label, value, tone) => h("li", { class: `sim-status__item is-${tone}` }, h("span", {}, label), h("b", {}, value));
      const list = h("ul", { class: "sim-status" },
        line(t("sim.status.cooling"), sum.cooling ? t("sim.status.coolingBad", { count: sum.cooling }) : t("sim.status.ok"), sum.cooling ? "warn" : "ok"),
        line(t("sim.status.warping"), t(`sim.verdict.${(sum.verdict ?? "fine").replace(/ /g, "_")}`), sum.warpBad ? "warn" : "ok"),
        line(t("sim.status.issues"), String(sum.issues), sum.issues ? "warn" : "ok"),
        ...result.extra.map((x) => x.status && line(x.status.label, x.status.value, x.status.tone ?? "ok")).filter(Boolean));
      body.append(list, h("p", { class: "ui-hint" }, result.detailed
        ? t(result.source === "file" ? "sim.basis.file" : "sim.basis.orca", { minutes: Math.round(result.gcode.print_time_min) })
        : result.gcodeError ? t("sim.basis.wrongFile", { reason: result.gcodeError }) : t("sim.basis.ours")));
      const grip = result.warp.adhesion;
      const curl = result.warp.range?.curl_mm;
      if (curl) body.append(h("p", {}, t("ranges.curl", { value: result.warp.curl_mm, low: curl[0], high: curl[1] })));
      if (curl || grip?.range) body.append(h("p", { class: "ui-hint" }, t("ranges.hint")));
      if (grip) {
        if (grip.range) {
          const verdict = { key: grip.may_lift ? "ranges.peel.lift" : "ranges.peel.hold" };
          body.append(h("p", {}, t(grip.range[0] !== grip.range[1] ? "ranges.peel.both" : "ranges.peel.same", { verdict })));
        }
        if (grip.detail) body.append(h("p", { class: "ui-hint" }, grip.detail));
        const note = grip.shear_source === "calibrated"
          ? (grip.calibration_temp_differs ? "calibratedTempDiffers" : "calibrated")
          : (grip.assumed_surface ? "assumed" : "estimated");
        body.append(h("p", { class: "ui-hint" }, t(`sim.adhesion.${note}`, { c: grip.calibration_temp_c })));
      }
      for (const sim of extra) sim.panel?.({ body, store, services, value: extraValue(result, sim), overlay });
    } else {
      body.append(h("p", { class: "ui-hint" }, t("sim.skipNote")));
    }

    // The overlay switch on the stage: Heat and the warping views need a result; Overhangs works from the shape.
    const choices = [{ value: "normal", label: t("sim.view.normal") }];
    if (fresh) choices.push({ value: "heat", label: t("sim.view.heat") }, { value: "warp", label: t("sim.view.warp") }, { value: "stress", label: t("sim.view.stress") });
    choices.push({ value: "overhang", label: t("sim.view.overhang") });
    if (!choices.some((c) => c.value === overlay)) overlay = "normal";
    if (overlay !== "heat") for (const sim of extra) sim.clearHeat?.(services);
    const legend = h("div", { class: "sim-legend" });
    const bar = h("div", { class: "sim-views" },
      SegmentedControl({ label: t("sim.view.label"), size: "small", options: choices, value: overlay, onChange: (v) => {
        const prev = overlay;
        const toHeat = v === "heat" && prev !== "heat";
        overlay = v;
        if (v === "overhang" && prev !== "overhang") services.stage?.view("under");
        else if (prev === "overhang" && v !== "overhang" && services.stage?.camera?.position?.z < 0) services.stage.view("iso");
        services.rerender();
        if (toHeat) requestAnimationFrame(() => document.querySelector(".heat-controls")?.scrollIntoView({ behavior: "smooth", block: "start" }));
      } }));
    if (overlay === "warp") {
      bar.append(SegmentedControl({ label: t("sim.view.size"), size: "small", value: warpSize,
        options: [{ value: "true", label: t("sim.view.true") }, { value: "big", label: t("sim.view.big") }], onChange: (v) => { warpSize = v; services.rerender(); } }));
    }
    stage.append(bar);
    showOverlay(store, services, legend);
    if (legend.childNodes.length) stage.append(legend);
  },
  next({ store }) {
    const result = store.result("simulate");
    if (!result || store.isStale("simulate")) return null;
    const n = summarize(result).problems;
    return n ? { label: t("sim.review", { count: n }), to: "problems" } : { label: t("sim.allClear"), to: "done" };
  },
  leave({ services }) { clearOverlay(services); },
});
