// Simulation step (plan §12), its heat part (T-478): the chosen option's sliced G-code goes through the heat model
// and the cooling plan (api.optimize_cooling with detail). On the stage: the window map (blue too cold, green fine,
// red too hot; before / after the plan), the importance map, or the G-code lines coloured by how their speed
// changed with markers where the fan changes; a layer slider plays the print back. In the panel: the summary card,
// how sure the limits are, a timeline against height, why a clicked spot changed, and the opt-in "Optimize my
// G-code directly". Warping and stress overlays and slicing in the background come with T-560.
import { h } from "../ui/dom.js";
import { Button } from "../ui/button.js";
import { SegmentedControl } from "../ui/choice.js";
import { ProgressCard } from "../ui/progress.js";
import { registerStep } from "../shell.js";
import { t } from "../i18n.js";
import { HeatView, heatColors } from "../viewport/heatview.js";
import { optimizeArgs } from "./args.js";
import { FADED_BELOW, WINDOW, byLayer, decode, explain, series, summary } from "./heatlogic.js";

const PLAY = "M8 5v14l11-7z";
const PAUSE = ["M7 5h4v14H7z", "M13 5h4v14h-4z"];
const STAGES = ["asSliced", "plan", "check"];
const SVG = "http://www.w3.org/2000/svg";

let gcode = null; // {id, name, bytes, info, up} the sliced file for the chosen option
let nextId = 1;
let run = null; // {started, done, share, card, timer} while the heat check runs
let error = null;
let fixing = null; // "busy" | {ok, problems} for "Optimize my G-code directly"
const view = { overlay: "heat", which: "after", layer: Infinity, picked: null, playing: null };
let shown = null; // {result, decoded, layers} drawn on the stage now

/** The engine profile for the chosen option: its material, nozzle and layer height on the user's printer. */
export function simProfile(s) {
  const o = s.option ?? {};
  return { ...optimizeArgs(s).profile, goal: s.goal || "balanced", ...(o.material ? { material: o.material } : {}),
    ...(o.nozzle ? { nozzle: String(o.nozzle) } : {}), ...(o.layer_height ? { layer_height: o.layer_height } : {}) };
}

const current = (store) => {
  const r = store.result("simulate");
  return r && gcode && r.gcode === gcode.id && !store.isStale("simulate") ? r.value : null;
};

async function loadFile(file, store, services) {
  error = null;
  fixing = null;
  const bytes = new Uint8Array(await file.arrayBuffer());
  const s = store.get();
  try {
    const info = await services.engine.call("load_gcode", { up: s.option.up, profile: simProfile(s) }, bytes);
    gcode = { id: nextId++, name: file.name, bytes, info, up: s.option.up };
  } catch (err) {
    error = t("heat.gcodeFailed", { reason: err?.message ?? String(err) });
  }
  services.rerender();
}

async function check(store, services) {
  const s = store.get();
  const deps = store.deps("model", "option", "profile", "goal", "loads");
  error = null;
  fixing = null;
  run = { started: performance.now(), done: 0, share: 0 };
  services.onProgress = (fraction) => {
    if (!run) return;
    run.share = Math.max(run.share, fraction ?? 0);
    run.done = run.share < 0.34 ? 0 : run.share < 0.67 ? 1 : 2;
    run.card?.set({ done: run.done, share: run.share, elapsed: (performance.now() - run.started) / 1000 });
  };
  run.timer = setInterval(() => run?.card?.set({ done: run.done, share: run.share, elapsed: (performance.now() - run.started) / 1000 }), 500);
  services.rerender();
  const id = gcode.id;
  try {
    // A restarted engine (Cancel anywhere) has lost the G-code: give it again, it is quick next to the heat runs.
    await services.engine.call("load_gcode", { up: gcode.up, profile: simProfile(s) }, gcode.bytes);
    const result = await services.engine.call("optimize_cooling", { up: gcode.up, profile: simProfile(s), loads: optimizeArgs(s).loads, detail: true });
    store.setResult("simulate", { value: result, gcode: id }, deps);
    Object.assign(view, { which: "after", layer: Infinity, picked: null });
  } catch (err) {
    const cancelled = err?.name === "Cancelled" || err?.constructor?.name === "Cancelled";
    if (!cancelled) error = t("heat.failed", { reason: err?.message ?? String(err) });
  } finally {
    clearInterval(run?.timer);
    run = null;
    services.rerender();
  }
}

async function fixGcode(store, services) {
  const s = store.get();
  fixing = "busy";
  services.rerender();
  try {
    await services.engine.call("load_gcode", { up: gcode.up, profile: simProfile(s) }, gcode.bytes);
    const r = await services.engine.call("optimize_gcode", { up: gcode.up, profile: simProfile(s), loads: optimizeArgs(s).loads }, gcode.bytes);
    fixing = r;
    if (r.ok) download(r.data, gcode.name.replace(/(\.gcode)?$/i, "_cooled.gcode"));
  } catch (err) {
    fixing = { ok: false, problems: [err?.message ?? String(err)] };
  }
  services.rerender();
}

function download(data, name) {
  const url = URL.createObjectURL(new Blob([data], { type: "text/x-gcode" }));
  const a = h("a", { href: url, download: name });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

// ---- the stage ----

function heatView(services) {
  services.heatView ??= new HeatView(services.stage);
  return services.heatView;
}

/** Draw a result on the stage once; later renders only recolour, hide or cut it. */
function draw(services, result) {
  const hv = heatView(services);
  if (shown?.result !== result) {
    const d = decode(result);
    const { order, upto } = byLayer(d.layer);
    services.stage.orientBy(result.rotation);
    hv.setSpots(d.points, d.cell, order, upto);
    const c = heatColors();
    hv.setLines(d.lines.segments, d.lines.layer, (i) => {
      const f = d.lines.factor[i];
      return f < 0.99 ? c.slower : f > 1.01 ? c.faster : c.same;
    });
    hv.setMarks(d.fanMarks, Math.max(0.4, d.cell * 0.3), c.fan);
    hv.onPick = (i) => { view.picked = i; services.rerender(); };
    shown = { result, decoded: d, layers: upto.length };
  }
  const d = shown.decoded, c = heatColors();
  if (view.overlay === "importance") {
    hv.colorSpots((i) => c.lowImportance.clone().lerp(c.highImportance, Math.min(1, Math.max(0, (d.importance[i] - 0.2) / 0.8))));
  } else {
    const w = d[view.which].window;
    hv.colorSpots((i) => {
      const col = c[WINDOW[w[i]]];
      return w[i] && d.importance[i] < FADED_BELOW ? col.clone().lerp(c.faded, 0.65) : col;
    });
  }
  hv.show(view.overlay === "lines" ? "lines" : view.overlay === "normal" ? null : "spots");
  hv.upTo(view.layer);
  hv.pick(view.overlay === "heat" || view.overlay === "importance" ? view.picked : null);
  services.stage.setXray(view.overlay !== "normal");
}

let watching = false;
/** Leaving the step takes the overlay away and stops playback. */
function watchLeave(store, services) {
  if (watching) return;
  watching = true;
  store.subscribe((s, changed) => {
    if (!changed.includes("step") || s.step === "sim") return;
    stopPlay();
    services.heatView?.clear();
    shown = null;
    services.stage.setXray(false);
  });
}

function stopPlay() {
  clearInterval(view.playing);
  view.playing = null;
}

// ---- the panel ----

function setupStrip(o, go) {
  const bits = [o.orientation, o.material_name ?? o.material, `${o.nozzle} mm`, t("heat.walls", { count: o.walls }), `${Math.round(o.infill * 100)} %`];
  return h("div", { class: "heat-strip" }, h("span", { class: "heat-strip__text" }, bits.filter(Boolean).join(" · ")),
    h("button", { type: "button", class: "ui-link", onclick: () => go("options") }, t("heat.change")));
}

function gcodeBlock(store, services) {
  const input = h("input", { type: "file", accept: ".gcode,.gco,.g,.3mf", class: "heat-file", "aria-label": t("heat.gcodePick"),
    onchange: (e) => e.target.files[0] && loadFile(e.target.files[0], store, services) });
  const pick = Button({ label: gcode ? t("heat.gcodeOther") : t("heat.gcodePick"), variant: gcode ? "ghost" : "secondary", size: gcode ? "small" : null, onClick: () => input.click() });
  if (!gcode) return h("section", { class: "heat-block" }, h("div", { class: "ui-label" }, t("heat.gcodeTitle")), h("p", { class: "ui-hint" }, t("heat.gcodeWhy")), input, pick);
  const i = gcode.info;
  return h("section", { class: "heat-block" }, h("div", { class: "ui-label" }, t("heat.gcodeTitle")),
    h("div", { class: "heat-file-row" }, h("span", {}, h("b", {}, gcode.name), " ",
      h("span", { class: "ui-hint num" }, t("heat.gcodeInfo", { layers: i.layers, min: i.print_time_min }))), input, pick));
}

function legend(overlay) {
  const items = overlay === "importance" ? [["low", "heat.legend.low"], ["high", "heat.legend.high"]]
    : overlay === "lines" ? [["slower", "heat.legend.slower"], ["faster", "heat.legend.faster"], ["same", "heat.legend.same"], ["fan", "heat.legend.fan"]]
      : [["cold", "heat.legend.cold"], ["ok", "heat.legend.ok"], ["hot", "heat.legend.hot"], ["slow", "heat.legend.slow"], ["faded", "heat.legend.faded"]];
  return h("ul", { class: "heat-legend" }, items.map(([cls, key]) => h("li", {}, h("i", { class: `heat-dot heat-dot--${cls}` }), t(key))));
}

function layerLabel(d, k) {
  const top = shown.layers - 1;
  const at = Math.min(k, top);
  return t("heat.layerAt", { layer: at + 1, layers: top + 1, z: Math.round(d.timeline.z[at] * 100) / 100 });
}

function playback(services) {
  const d = shown.decoded, top = shown.layers - 1;
  const value = Number.isFinite(view.layer) ? Math.min(view.layer, top) : top;
  const label = h("span", { class: "ui-hint num" }, layerLabel(d, value));
  const slider = h("input", { type: "range", min: "0", max: String(top), value: String(value), class: "heat-slider", "aria-label": t("heat.layers"),
    dataset: { focusKey: "heat-layer" } });
  const set = (k) => {
    view.layer = k >= top ? Infinity : k;
    slider.value = String(k);
    label.textContent = layerLabel(d, k);
    services.heatView.upTo(view.layer);
    marker(k);
  };
  slider.addEventListener("input", () => { stopPlay(); playBtn.replaceWith(playBtn = makePlay()); set(Number(slider.value)); });
  const makePlay = () => Button({ iconPath: view.playing ? PAUSE : PLAY, title: view.playing ? t("heat.pause") : t("heat.play"), variant: "secondary", size: "small",
    onClick: () => {
      if (view.playing) stopPlay();
      else {
        let k = Number(slider.value) >= top ? 0 : Number(slider.value);
        const step = Math.max(1, Math.round(top / 120));
        view.playing = setInterval(() => {
          k = Math.min(top, k + step);
          set(k);
          if (k >= top) { stopPlay(); playBtn.replaceWith(playBtn = makePlay()); }
        }, 80);
      }
      playBtn.replaceWith(playBtn = makePlay());
    } });
  let playBtn = makePlay();
  return h("section", { class: "heat-block" }, h("div", { class: "heat-play" }, playBtn, slider), label);
}

let marker = () => {};

/** One small line chart against height: ``lines`` [{points, cls}], an optional shaded ``band`` [lo, hi]. */
function chart(title, z, lines, { band, unit, max } = {}) {
  const W = 336, H = 92, L = 34, R = 6, T = 8, B = 18;
  const all = lines.flatMap((l) => l.points.map((p) => p[1])).concat(band ? band.flat().filter(Number.isFinite) : []);
  const z0 = z[0] ?? 0, z1 = z[z.length - 1] ?? 1;
  const lo = Math.min(0, ...all), hi = max ?? Math.max(1, ...all) * 1.05;
  const x = (v) => L + ((v - z0) / Math.max(z1 - z0, 1e-6)) * (W - L - R);
  const y = (v) => T + (1 - (v - lo) / Math.max(hi - lo, 1e-6)) * (H - T - B);
  const el = (tag, attrs) => {
    const e = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    return e;
  };
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, class: "heat-chart", role: "img", "aria-label": title });
  if (band) {
    const [bl, bh] = band;
    const top = z.map((v, k) => `${x(v)},${y(Math.min(hi, bh[k] ?? bh))}`);
    const bottom = z.map((v, k) => `${x(v)},${y(Math.max(lo, bl[k] ?? bl))}`).reverse();
    svg.append(el("polygon", { points: [...top, ...bottom].join(" "), class: "heat-band" }));
  }
  svg.append(el("line", { x1: L, x2: W - R, y1: y(lo), y2: y(lo), class: "heat-axis" }));
  for (const v of [lo, (lo + hi) / 2, hi]) {
    const tick = el("text", { x: L - 4, y: y(v) + 4, class: "heat-tick", "text-anchor": "end" });
    tick.textContent = String(Math.round(v));
    svg.append(tick);
  }
  for (const l of lines) {
    if (!l.points.length) continue;
    svg.append(el("polyline", { points: l.points.map(([a, b]) => `${x(a)},${y(b)}`).join(" "), class: l.cls }));
  }
  const unitText = el("text", { x: L, y: H - 4, class: "heat-tick" });
  unitText.textContent = unit;
  const zText = el("text", { x: W - R, y: H - 4, class: "heat-tick", "text-anchor": "end" });
  zText.textContent = `${Math.round(z1)} mm`;
  const mark = el("line", { y1: T, y2: H - B, class: "heat-marker" });
  svg.append(unitText, zText, mark);
  return { node: h("figure", { class: "heat-figure" }, h("figcaption", { class: "ui-hint" }, title), svg),
    at: (k) => { const v = x(z[Math.min(k, z.length - 1)]); mark.setAttribute("x1", v); mark.setAttribute("x2", v); } };
}

function timeline() {
  const d = shown.decoded, tl = d.timeline, z = Array.from(tl.z);
  const pair = (arr, scale = 1) => [{ points: series(tl.z, arr[0].map((v) => v * scale)), cls: "heat-line heat-line--before" },
    { points: series(tl.z, arr[1].map((v) => v * scale)), cls: "heat-line heat-line--after" }];
  const temp = chart(t("heat.chart.temp"), z, pair(tl.cover), { band: [z.map(() => tl.tg), Array.from(tl.limit)], unit: "°C" });
  const secs = chart(t("heat.chart.time"), z, pair(tl.seconds), { unit: "s" });
  const fan = chart(t("heat.chart.fan"), z, pair(tl.fan, 100), { unit: "%", max: 100 });
  const charts = [temp, secs, fan];
  marker = (k) => charts.forEach((c) => c.at(k));
  marker(Number.isFinite(view.layer) ? view.layer : shown.layers - 1);
  return h("details", { class: "heat-block heat-timeline", open: true },
    h("summary", { class: "ui-label" }, t("heat.chart.title")),
    h("p", { class: "ui-hint" }, h("i", { class: "heat-key heat-key--before" }), t("heat.chart.before"), " ", h("i", { class: "heat-key heat-key--after" }), t("heat.chart.after"),
      " ", h("i", { class: "heat-key heat-key--band" }), t("heat.chart.band")),
    charts.map((c) => c.node));
}

function whyCard(r, services) {
  const d = shown.decoded;
  return h("section", { class: "heat-why", "aria-live": "polite" },
    h("div", { class: "heat-why__head" }, h("b", {}, t("heat.why.title")),
      Button({ iconPath: "M6 6l12 12M18 6 6 18", title: t("heat.why.close"), variant: "ghost", size: "small", onClick: () => { view.picked = null; services.rerender(); } })),
    explain(d, r.gcode.plan, view.picked).map((m) => h("p", {}, t(m))));
}

function fixBlock(store, services) {
  const busy = fixing === "busy";
  const out = [Button({ label: busy ? t("heat.fixBusy") : t("heat.fix"), variant: "secondary", disabled: busy || Boolean(run), onClick: () => fixGcode(store, services) }),
    h("p", { class: "ui-hint" }, t("heat.fixHint"))];
  if (fixing && fixing !== "busy") {
    out.push(fixing.ok
      ? h("p", { class: "heat-ok", role: "status" }, t("heat.fixDone", { from: fixing.minutes[0], to: fixing.minutes[1] }))
      : h("div", { class: "drop-error", role: "alert" }, h("p", {}, t("heat.fixRefused")), h("ul", {}, fixing.problems.map((p) => h("li", {}, p)))));
  }
  return h("section", { class: "heat-block" }, out);
}

registerStep("sim", {
  render({ body, stage, store, go, services }) {
    watchLeave(store, services);
    const s = store.get();
    if (!s.option) {
      body.append(h("p", { class: "ui-hint" }, t("heat.noOption")), Button({ label: t("heat.toOptions"), variant: "secondary", onClick: () => go("options") }));
      return;
    }
    if (gcode && JSON.stringify(gcode.up) !== JSON.stringify(s.option.up)) gcode = null; // sliced for another orientation
    body.append(setupStrip(s.option, go), gcodeBlock(store, services));
    if (error) body.append(h("p", { class: "drop-error", role: "alert" }, error));
    const r = current(store);
    if (run) {
      const card = ProgressCard({ title: t("heat.running"), stages: STAGES.map((k) => t(`heat.stage.${k}`)), onCancel: () => services.engine.cancel(), onStop: () => {} });
      card.querySelector(".ui-progress__foot .ui-btn--secondary")?.remove();
      run.card = card;
      card.set({ done: run.done, share: run.share, elapsed: (performance.now() - run.started) / 1000 });
      stage.append(h("div", { class: "analyse-progress" }, card));
    }
    if (gcode) {
      body.append(Button({ label: run ? t("heat.runningShort") : r ? t("heat.again") : t("heat.run"), variant: r ? "ghost" : "primary", disabled: Boolean(run),
        onClick: () => check(store, services) }));
    }
    if (!r) {
      if (shown) { services.heatView?.clear(); shown = null; services.stage.setXray(false); }
      if (gcode && store.result("simulate") && store.isStale("simulate")) body.append(h("p", { class: "analyse-stale" }, t("heat.stale")));
      if (!run) services.stage.orient(s.option.up);
      return;
    }
    draw(services, r);
    const conf = { rough: "rough", "partly tuned": "partly", tuned: "tuned" }[r.confidence] ?? "rough";
    body.append(h("section", { class: "heat-summary" },
      h("div", { class: "heat-summary__head" }, h("b", {}, t("heat.sum.title")), h("span", { class: `heat-conf heat-conf--${conf}` }, t(`heat.confidence.${conf}`))),
      h("ul", {}, summary(r, shown.decoded).map((m) => h("li", {}, t(m)))),
      r.guessed ? h("p", { class: "ui-hint" }, t("heat.guessed")) : null));
    body.append(h("section", { class: "heat-block" }, h("div", { class: "ui-label" }, t("heat.show")),
      SegmentedControl({ label: t("heat.show"), value: view.overlay, size: "small",
        options: ["normal", "heat", "importance", "lines"].map((v) => ({ value: v, label: t(`heat.overlay.${v}`) })),
        onChange: (v) => { view.overlay = v; services.rerender(); } }),
      view.overlay === "heat" ? SegmentedControl({ label: t("heat.whichRun"), value: view.which, size: "small",
        options: [{ value: "before", label: t("heat.before") }, { value: "after", label: t("heat.after") }],
        onChange: (v) => { view.which = v; services.rerender(); } }) : null,
      view.overlay !== "normal" ? legend(view.overlay) : null,
      view.overlay === "heat" || view.overlay === "importance" ? h("p", { class: "ui-hint" }, t("heat.clickSpot")) : null));
    if (view.picked != null && (view.overlay === "heat" || view.overlay === "importance")) body.append(whyCard(r, services));
    body.append(playback(services), timeline(), fixBlock(store, services));
  },
});
