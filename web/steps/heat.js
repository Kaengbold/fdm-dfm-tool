// The heat model and the cooling plan in the Simulation step (T-478), joined to Run simulations through
// registerSimulation (web/steps/simulate.js, T-560). With the run's G-code (sliced in the browser or the user's own
// file), api.optimize_cooling(detail) simulates every spot as sliced and with the cooling plan. Then the step's Heat
// view shows the window map (blue too cold, green fine, red too hot; as sliced / with the plan), what matters most,
// or the G-code lines coloured by how their speed changed with markers where the fan changes; a layer slider plays
// the print back. In the panel: a summary card with how sure the limits are, a timeline against height, why a
// clicked spot changed, and the opt-in "Optimize my G-code directly" (the user's file stays as it is).
import { h } from "../ui/dom.js";
import { Button } from "../ui/button.js";
import { SegmentedControl } from "../ui/choice.js";
import { Cancelled } from "../engine.js";
import { t } from "../i18n.js";
import { HeatView, heatColors } from "../viewport/heatview.js";
import { optimizeArgs } from "./args.js";
import { registerSimulation } from "./simulate.js";
import { FADED_BELOW, WINDOW, byLayer, clock, decode, decodeReplay, explain, foundText, planText, problemLayers, series, snapAt, summary, tempAt, tempBand }
  from "./heatlogic.js";

const PLAY = "M8 5v14l11-7z";
const PAUSE = ["M7 5h4v14H7z", "M13 5h4v14h-4z"];
const SVG = "http://www.w3.org/2000/svg";

let fixing = null; // "busy" | {ok, problems, minutes} for "Optimize my G-code directly"
// mode: replay | window | importance | lines; run: the replay's run (before: as sliced, after: with the plan)
const view = { mode: "replay", which: "after", run: "before", layer: Infinity, picked: null, playing: null };
let shown = null; // {result, decoded, replay, layers} drawn on the stage now

/** Run simulations, after the print checks and the warping: the cooling plan on the run's G-code. Without a G-code
 * (our own slicer stood in) there is nothing to follow line by line, and the panel says so. */
async function runHeat({ engine, up, profile, detailed, state, gcode }) {
  if (!detailed || !gcode) return { needsGcode: true };
  const args = { up, profile: { ...profile, goal: state.goal || "balanced" }, loads: optimizeArgs(state).loads };
  fixing = null;
  Object.assign(view, { mode: "replay", which: "after", run: "before", layer: Infinity, picked: null });
  try {
    const result = await engine.call("optimize_cooling", { ...args, detail: true, ranges: true });
    const hot = result.before.too_hot + result.before.too_cold, left = result.gcode.too_hot + result.gcode.too_cold;
    const name = (state.part?.name ?? "part").replace(/\.[^.]+$/, "");
    return { result, gcode, args, name, status: { label: t("heat.status"), value: hot ? t("heat.statusFixed", { count: hot, left }) : t("heat.statusFine"), tone: left ? "warn" : "ok" } };
  } catch (err) {
    if (err instanceof Cancelled) throw err;
    return { error: err?.message ?? String(err), status: { label: t("heat.status"), value: t("heat.statusFailed"), tone: "warn" } };
  }
}

async function fixGcode(value, services) {
  fixing = "busy";
  services.rerender();
  try {
    // The G-code the run read may be gone (another file, a restarted engine): give it again, it is quick.
    await services.engine.call("load_gcode", { up: value.args.up, profile: value.args.profile }, value.gcode);
    const r = await services.engine.call("optimize_gcode", value.args, value.gcode);
    fixing = r;
    if (r.ok) download(r.data, `${value.name}_cooled.gcode`);
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

/** Draw a result on the stage once (the part turned the way the engine prints it); later calls only recolour, hide
 * or cut it. */
function showHeat({ services, value, legend: stageLegend }) {
  const result = value?.result;
  if (!result) return false;
  services.heatView ??= new HeatView(services.stage);
  const hv = services.heatView;
  if (shown?.result !== result) {
    const d = decode(result);
    const { upto } = byLayer(d.layer);
    services.stage.orientBy(result.rotation);
    hv.setPaths(d.lines.segments, d.lines.width, d.lines.height, d.lines.layer, d.lines.spot);
    hv.setSpots(d.points, d.cell, d.lines.height[0] || 0.2);
    const c = heatColors();
    hv.setMarks(d.fanMarks, Math.max(0.4, d.cell * 0.3), c.fan);
    hv.onPick = (i) => { view.picked = i; services.rerender(); };
    shown = { result, decoded: d, replay: decodeReplay(result), layers: upto.length };
    if (!shown.replay && view.mode === "replay") view.mode = "window";
  }
  const d = shown.decoded, c = heatColors();
  hv.upTo(view.layer);
  if (view.mode === "replay") {
    paintReplay(hv);
  } else if (view.mode === "lines") {
    hv.colorPaths((i) => {
      const f = d.lines.factor[i];
      return f < 0.99 ? c.slower : f > 1.01 ? c.faster : c.same;
    });
  } else if (view.mode === "importance") {
    hv.colorPaths((_, s) => (s < 0 ? c.faded : c.lowImportance.clone().lerp(c.highImportance, Math.min(1, Math.max(0, (d.importance[s] - 0.2) / 0.8)))));
  } else {
    const w = d[view.which].window;
    hv.colorPaths((_, s) => {
      if (s < 0) return c.faded;
      const col = c[WINDOW[w[s]]];
      return w[s] && d.importance[s] < FADED_BELOW ? col.clone().lerp(c.faded, 0.65) : col;
    });
  }
  hv.showMarks(view.mode === "lines");
  hv.pick(view.mode === "window" || view.mode === "importance" ? view.picked : null);
  // The print the simulation ran on, not the model: the part's own surface steps aside (T-587).
  if (services.stage.part) services.stage.part.group.visible = false;
  if (stageLegend) {
    stageLegend.replaceChildren(h("b", {}, t(`heat.stageLegend.${view.mode}`)), legend(view.mode));
  }
  services.stage.render();
  return true;
}

/** Another view, or leaving the step: the overlay goes, the part stands as it prints again (as loaded before an option). */
function clearHeat(services) {
  stopPlay();
  if (!shown) return;
  services.heatView?.clear();
  if (services.stage.part) services.stage.part.group.visible = true;
  services.stage.rest();
  shown = null;
}

/** The replay's colours at the current layer limit: each bead by its spot's temperature when that layer was done
 * (T-588): dark blue set, green to yellow good to build on, orange to red too soft to build on. */
function paintReplay(hv) {
  const d = shown.decoded, rp = shown.replay, c = heatColors();
  const run = view.run === "after" ? 1 : 0, j = snapAt(rp, Number.isFinite(view.layer) ? view.layer : Infinity), tg = d.timeline.tg;
  hv.colorPaths((_, s) => {
    const temp = s < 0 ? NaN : tempAt(rp, run, j, s);
    if (!Number.isFinite(temp)) return c.faded;
    const { band, f } = tempBand(temp, tg, d.limit[s], rp.lo, rp.hi);
    return band === "set" ? c.set.clone().lerp(c.cold, f) : band === "warm" ? c.ok.clone().lerp(c.warm, f) : c.warm.clone().lerp(c.hot, 0.5 + f / 2);
  }, true);
}

function stopPlay() {
  clearInterval(view.playing);
  view.playing = null;
}

// ---- the panel ----

function legend(overlay) {
  const items = overlay === "replay" ? [["set", "heat.legend.set"], ["ok", "heat.legend.warm"], ["hot", "heat.legend.soft"]]
    : overlay === "importance" ? [["low", "heat.legend.low"], ["high", "heat.legend.high"]]
    : overlay === "lines" ? [["slower", "heat.legend.slower"], ["faster", "heat.legend.faster"], ["same", "heat.legend.same"], ["fan", "heat.legend.fan"]]
      : [["cold", "heat.legend.cold"], ["ok", "heat.legend.ok"], ["hot", "heat.legend.hot"], ["slow", "heat.legend.slow"], ["faded", "heat.legend.faded"]];
  return h("ul", { class: "heat-legend" }, items.map(([cls, key]) => h("li", {}, h("i", { class: `heat-dot heat-dot--${cls}` }), t(key))));
}

function layerLabel(d, k) {
  const top = shown.layers - 1;
  const at = Math.min(k, top);
  const params = { layer: at + 1, layers: top + 1, z: Math.round(d.timeline.z[at] * 100) / 100 };
  if (view.mode !== "replay" || !shown.replay) return t("heat.layerAt", params);
  const rp = shown.replay, run = view.run === "after" ? 1 : 0, j = snapAt(rp, at);
  return t("heat.replay.at", { ...params, time: clock(rp.t[run][j]), fan: Math.round(rp.fan[run][j] * 100) });
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
    if (view.mode === "replay") paintReplay(services.heatView);
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

/** Play the replay from the first layer (``run``: before / after). */
function replayFrom(run, services) {
  stopPlay();
  Object.assign(view, { mode: "replay", run, layer: 0, picked: null });
  services.rerender();
  document.querySelector(".heat-play button")?.click();
}

/** The replay as a short story (T-588): watch it print, what we found, what we change; the result is the summary. */
function story(r, services) {
  const d = shown.decoded;
  const found = problemLayers(d);
  const jump = (k) => { stopPlay(); Object.assign(view, { run: "before", layer: k }); services.rerender(); };
  return [
    h("section", { class: "heat-block heat-story" }, h("div", { class: "ui-label" }, t("heat.story.watch")),
      SegmentedControl({ label: t("heat.whichRun"), value: view.run, size: "small",
        options: [{ value: "before", label: t("heat.before") }, { value: "after", label: t("heat.after") }],
        onChange: (v) => { view.run = v; services.rerender(); } }),
      legend("replay"), h("p", { class: "ui-hint" }, t("heat.story.watchHint"))),
    playback(services),
    h("section", { class: "heat-block heat-story" }, h("div", { class: "ui-label" }, t("heat.story.found")),
      found.length ? h("ul", { class: "heat-found" }, found.map((p) => h("li", {},
        h("span", {}, t(foundText(p))),
        Button({ label: t("heat.story.show"), size: "small", variant: "ghost", onClick: () => jump(p.layer) }))))
        : h("p", { class: "ui-hint" }, t("heat.story.none"))),
    h("section", { class: "heat-block heat-story" }, h("div", { class: "ui-label" }, t("heat.story.change")),
      r.gcode.plan.length ? h("ul", { class: "heat-changes" }, planText(r.gcode.plan).map((m) => h("li", {}, t(m))))
        : h("p", { class: "ui-hint" }, t("heat.story.noChange")),
      r.gcode.plan.length ? Button({ label: t("heat.story.replayPlan"), size: "small", variant: "secondary", onClick: () => replayFrom("after", services) }) : null),
  ];
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

function fixBlock(value, services) {
  const busy = fixing === "busy";
  const out = [Button({ label: busy ? t("heat.fixBusy") : t("heat.fix"), variant: "secondary", disabled: busy, onClick: () => fixGcode(value, services) }),
    h("p", { class: "ui-hint" }, t("heat.fixHint"))];
  if (value.gcode && String.fromCharCode(...value.gcode.subarray(0, 4)) === "GCDE") {
    out.push(h("p", { class: "ui-hint" }, t("heat.fixBinary")));
  }
  if (fixing && fixing !== "busy") {
    out.push(fixing.ok
      ? h("p", { class: "heat-ok", role: "status" }, t("heat.fixDone", { from: fixing.minutes[0], to: fixing.minutes[1] }))
      : h("div", { class: "drop-error", role: "alert" }, h("p", {}, t("heat.fixRefused")), h("ul", {}, fixing.problems.map((p) => h("li", {}, p)))));
  }
  return h("section", { class: "heat-block" }, out);
}

/** The panel section under the status list. The heat views (switch, key, playback) only while the Heat view is on. */
function panel({ body, services, value, overlay }) {
  if (!value) return;
  if (value.needsGcode) {
    body.append(h("p", { class: "ui-hint" }, t("heat.needsGcode")));
    return;
  }
  if (value.error) {
    body.append(h("p", { class: "drop-error", role: "alert" }, t("heat.failed", { reason: value.error })));
    return;
  }
  const r = value.result;
  const before = r.before;
  const cover = before.range?.cover_c?.median;
  const bond = before.range?.bond_median;
  const d = shown?.result === r ? shown.decoded : decode(r);
  const conf = { rough: "rough", "partly tuned": "partly", tuned: "tuned" }[r.confidence] ?? "rough";
  body.append(h("section", { class: "heat-summary" },
    h("div", { class: "heat-summary__head" }, h("b", {}, t("heat.sum.title")), h("span", { class: `heat-conf heat-conf--${conf}` }, t(`heat.confidence.${conf}`))),
    h("ul", {}, summary(r, d).map((m) => h("li", {}, t(m))),
      cover ? h("li", {}, t("ranges.cover", { value: before.cover_c.median, low: cover[0], high: cover[1] })) : null,
      bond ? h("li", {}, t("ranges.bond", { value: Math.round(before.bond_median * 100), low: Math.round(bond[0] * 100), high: Math.round(bond[1] * 100) })) : null),
    cover || bond ? h("p", { class: "ui-hint" }, t("ranges.hint")) : null,
    r.guessed ? h("p", { class: "ui-hint" }, t("heat.guessed")) : null));
  if (overlay === "heat") showHeat({ services, value }); // the stage is drawn after the panel: draw it now
  if (overlay !== "heat" || shown?.result !== r) {
    body.append(h("p", { class: "ui-hint" }, t("heat.seeHeat")), fixBlock(value, services));
    return;
  }
  body.append(h("section", { class: "heat-block heat-controls" }, h("div", { class: "ui-label" }, t("heat.show")),
    SegmentedControl({ label: t("heat.show"), value: view.mode,
      options: [...(shown.replay ? ["replay"] : []), "window", "importance", "lines"].map((v) => ({ value: v, label: t(`heat.mode.${v}`) })),
      onChange: (v) => { view.mode = v; services.rerender(); } }),
    view.mode === "window" ? SegmentedControl({ label: t("heat.whichRun"), value: view.which, size: "small",
      options: [{ value: "before", label: t("heat.before") }, { value: "after", label: t("heat.after") }],
      onChange: (v) => { view.which = v; services.rerender(); } }) : null,
    view.mode !== "replay" ? legend(view.mode) : null,
    view.mode === "window" || view.mode === "importance" ? h("p", { class: "ui-hint" }, t("heat.clickSpot")) : null));
  if (view.mode === "replay") {
    body.append(...story(r, services), timeline(), fixBlock(value, services));
    return;
  }
  if (view.picked != null && view.mode !== "lines") body.append(whyCard(r, services));
  body.append(playback(services), timeline(), fixBlock(value, services));
}

registerSimulation({ name: "heat", stage: "heat", run: runHeat, panel, showHeat, clearHeat });
