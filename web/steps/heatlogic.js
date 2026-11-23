// Pure logic of the simulation step's heat views (T-478): reads api.optimize_cooling(detail) into typed arrays and
// turns it into messages ({key, params}, shown with t()): why a spot changed, the summary card, what limits the
// print. No DOM: unit-tested under Node (core/tests/test_sim_step.py).

export const WINDOW = ["ok", "hot", "cold", "slow"]; // api window codes 0..3: fine, too hot, too cold, needless slowdown
export const FADED_BELOW = 0.35; // importance under this: a spot outside its window there is an accepted risk

const bytes = (b) => (b instanceof Uint8Array ? b : new Uint8Array(b));
const f32 = (b) => new Float32Array(bytes(b).slice().buffer);
const i32 = (b) => new Int32Array(bytes(b).slice().buffer);
const u8 = (b) => bytes(b).slice();

/** The result's byte arrays as typed arrays; ``before`` / ``after`` hold the per-spot values of each run. */
export function decode(r) {
  const s = r.spots, tl = r.timeline, lines = r.lines;
  const run = (i) => ({ cover: f32(s.cover_c[i]), coverS: f32(s.cover_s[i]), bond: f32(s.bond[i]), window: u8(s.window[i]) });
  return {
    points: f32(s.points), layer: i32(s.layer), limit: f32(s.limit_c), importance: f32(s.importance), reason: u8(s.reason),
    reasons: s.reasons, cell: s.cell_mm, before: run(0), after: run(1),
    timeline: { z: f32(tl.z), seconds: tl.seconds.map(f32), fan: tl.fan.map(f32), cover: tl.cover_c.map(f32), limit: f32(tl.limit_c), tg: tl.tg_c },
    lines: { segments: f32(lines.segments), factor: f32(lines.factor), layer: i32(lines.layer), width: f32(lines.width), height: f32(lines.height),
      spot: i32(lines.spot), all: lines.all },
    fanMarks: r.fan_marks ?? [],
  };
}

/** Spot counts per window state, and how many of the outside ones are accepted risks (faded). */
export function counts(d, which) {
  const w = d[which].window, out = { ok: 0, hot: 0, cold: 0, slow: 0, accepted: 0 };
  for (let i = 0; i < w.length; i++) {
    out[WINDOW[w[i]]] += 1;
    if (w[i] && d.importance[i] < FADED_BELOW) out.accepted += 1;
  }
  return out;
}

/** The G-code plan's row that changes how long spot layer ``k`` cools: the covering layer's (k + 1) first, else k's. */
export function planFor(plan, k) {
  const has = (n) => plan.find((r) => r.layers[0] <= n && n <= r.layers[1]);
  return has(k + 1) ?? has(k) ?? null;
}

/** What a plan row does, as a list of messages. */
export function actions(row) {
  if (!row) return [];
  const out = [];
  if (row.fan != null) out.push({ key: "heat.do.fan", params: { fan: Math.round(row.fan * 100) } });
  if (row.aux_fan != null) out.push({ key: "heat.do.auxFan", params: { fan: Math.round(row.aux_fan * 100) } });
  if (row.slower < 1) out.push({ key: "heat.do.slower", params: { pct: Math.round(row.slower * 100) } });
  if (row.faster > 1) out.push({ key: "heat.do.faster" });
  if (row.park_s > 0) out.push({ key: "heat.do.park", params: { s: row.park_s } });
  return out;
}

const r0 = (v) => Math.round(v);
const r1 = (v) => Math.round(v * 10) / 10;

function state(d, run, i) {
  const w = WINDOW[d[run].window[i]];
  if (w === "hot") return { key: "heat.why.hot", params: { limit: r0(d.limit[i]) } };
  if (w === "cold") return { key: "heat.why.cold", params: { bond: r0(d[run].bond[i] * 100) } };
  if (w === "slow") return { key: "heat.why.slow" };
  return { key: "heat.why.ok" };
}

/** Why spot ``i`` is what it is and what the plan did about it: a list of messages, one sentence each. */
export function explain(d, plan, i) {
  const k = d.layer[i];
  const z = d.points[i * 3 + 2];
  const out = [{ key: "heat.why.where", params: { layer: k + 1, z: r1(z) } }];
  const b = d.before, a = d.after;
  if (!Number.isFinite(b.cover[i])) {
    out.push({ key: "heat.why.top" });
    return out;
  }
  out.push({ key: "heat.why.before", params: { s: r1(b.coverS[i]), c: r0(b.cover[i]) } }, state(d, "before", i));
  const did = actions(planFor(plan, k));
  if (did.length) {
    out.push({ key: "heat.why.change", params: { actions: did } });
    if (Number.isFinite(a.cover[i])) out.push({ key: "heat.why.after", params: { s: r1(a.coverS[i]), c: r0(a.cover[i]) } }, state(d, "after", i));
  } else {
    out.push({ key: b.window[i] ? "heat.why.noFix" : "heat.why.noChange" });
  }
  const reason = d.reasons[d.reason[i]] ?? "inside";
  out.push({ key: "heat.why.matters", params: { what: { key: `heat.reason.${reason}` } } });
  if (b.window[i] && d.importance[i] < FADED_BELOW) out.push({ key: "heat.why.accepted" });
  return out;
}

const CAUSES = ["overhang", "bridge", "small_perimeter", "volumetric_cap", "layer_time"];

/** The summary card: time, spots fixed, bonding, the trade-off, what limits the print. ``r``: the raw result. */
export function summary(r, d) {
  const b = r.before, g = r.gcode;
  const lines = [];
  const before = counts(d, "before"), after = counts(d, "after");
  if (!g.plan.length && !before.hot && !before.cold && !before.slow) {
    lines.push({ key: "heat.sum.nothing" });
  } else if (!g.plan.length) {
    lines.push({ key: "heat.sum.cantFix", params: { count: before.hot + before.cold + before.slow } });
  } else {
    const delta = g.print_min - b.print_min;
    lines.push(delta <= -0.05 ? { key: "heat.sum.faster", params: { min: r1(-delta) } }
      : delta >= 0.05 ? { key: "heat.sum.slower", params: { min: r1(delta) } } : { key: "heat.sum.sameTime" });
    if (before.hot) lines.push({ key: "heat.sum.hot", params: { count: before.hot, left: after.hot } });
    if (before.cold || before.slow) lines.push({ key: "heat.sum.cold", params: { count: before.cold + before.slow, left: after.cold + after.slow } });
    if (b.bond_median != null && g.bond_median != null && Math.abs(g.bond_median - b.bond_median) >= 0.01) {
      lines.push({ key: "heat.sum.bond", params: { from: r0(b.bond_median * 100), to: r0(g.bond_median * 100) } });
    }
    if (after.hot && after.hot < before.hot) lines.push({ key: "heat.sum.tradeoff" });
  }
  if (after.accepted) lines.push({ key: "heat.sum.accepted", params: { count: after.accepted } });
  if (r.aux_fan_estimated) lines.push({ key: "heat.sum.auxEstimated" });
  const slow = Object.entries(r.slowdown_min ?? {}).filter(([c]) => CAUSES.includes(c)).sort((x, y) => y[1] - x[1]);
  if (slow.length) lines.push({ key: "heat.sum.limit", params: { what: { key: `heat.cause.${slow[0][0]}` }, min: slow[0][1] } });
  return lines;
}

/** Per layer, the points of a timeline chart (x: height) for one series; NaN gaps are dropped. */
export function series(z, values) {
  const out = [];
  for (let k = 0; k < z.length; k++) if (Number.isFinite(values[k]) && Number.isFinite(z[k])) out.push([z[k], values[k]]);
  return out;
}

/** Spot indices sorted by layer, and per layer the count of spots up to it (for the layer slider). */
export function byLayer(layer) {
  const order = Array.from(layer.keys()).sort((x, y) => layer[x] - layer[y] || x - y);
  const top = layer.reduce((m, k) => Math.max(m, k), -1); // not Math.max(...layer): too many arguments on a big part
  const upto = new Int32Array(top + 1);
  for (const i of order) upto[layer[i]] += 1;
  for (let k = 1; k <= top; k++) upto[k] += upto[k - 1];
  return { order, upto };
}

// ---- the replay (T-588) ----

/** The replay's typed arrays: every spot's temperature when each kept layer is done, as sliced (run 0) and with the
 * plan (run 1); null when the engine sent none. */
export function decodeReplay(r) {
  const p = r.replay;
  if (!p) return null;
  return { layer: i32(p.layer), start: i32(p.start), t: p.t_s.map(f32), fan: p.fan.map(f32), temp: p.temp.map(u8), lo: p.lo_c, hi: p.hi_c };
}

/** The snapshot shown with layers up to ``k``: the last one kept at or below it (0 before the first). */
export function snapAt(rp, k) {
  let j = 0;
  for (let i = 0; i < rp.layer.length && rp.layer[i] <= k; i++) j = i;
  return j;
}

/** Spot ``s``'s temperature (°C) in snapshot ``j`` of ``run`` (0 as sliced, 1 with the plan); NaN: not printed yet. */
export function tempAt(rp, run, j, s) {
  const from = rp.start[j], to = j + 1 < rp.start.length ? rp.start[j + 1] : rp.temp[run].length;
  return s < to - from ? rp.lo + (rp.temp[run][from + s] / 255) * (rp.hi - rp.lo) : NaN;
}

/** Seconds as m:ss (h:mm:ss past an hour), for the replay's clock. */
export function clock(seconds) {
  const s = Math.max(0, Math.round(seconds)), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${x}` : `${m}:${x}`;
}

/** The layers with the most spots outside their window as sliced (only those that matter), worst first, at most
 * ``max``: [{layer, z, hot, cold}]. */
export function problemLayers(d, max = 4) {
  const per = new Map();
  for (let i = 0; i < d.layer.length; i++) {
    const w = WINDOW[d.before.window[i]];
    if (w === "ok" || d.importance[i] < FADED_BELOW) continue;
    const k = d.layer[i];
    const p = per.get(k) ?? { layer: k, z: r1(d.points[i * 3 + 2]), hot: 0, cold: 0 };
    if (w === "hot") p.hot += 1;
    else p.cold += 1;
    per.set(k, p);
  }
  return [...per.values()].sort((a, b) => b.hot + b.cold - (a.hot + a.cold) || a.layer - b.layer).slice(0, max);
}

/** One problem layer in words. */
export function foundText(p) {
  const params = { layer: p.layer + 1, z: p.z, hot: p.hot, cold: p.cold, count: p.hot || p.cold };
  return { key: p.hot && p.cold ? "heat.story.foundBoth" : p.hot ? "heat.story.foundHot" : "heat.story.foundCold", params };
}

/** The G-code plan, one sentence per row of layers with the same change. */
export function planText(plan) {
  return plan.map((row) => ({ key: row.layers[0] === row.layers[1] ? "heat.story.changeOne" : "heat.story.changeRows",
    params: { from: row.layers[0] + 1, to: row.layers[1] + 1, z0: r1(row.z_mm[0] ?? 0), z1: r1(row.z_mm[1] ?? 0), actions: actions(row) } }));
}

/** Where a temperature sits for a spot: "set" below the glass transition (too cold for the next layer to bond
 * well), "warm" between that and the spot's sag limit (good to build on), "hot" above it (too soft to build on),
 * with how far along that band it is (0–1). */
export function tempBand(c, tg, limit, lo, hi) {
  if (c < tg) return { band: "set", f: Math.max(0, Math.min(1, (c - lo) / Math.max(tg - lo, 1e-6))) };
  if (c < limit) return { band: "warm", f: (c - tg) / Math.max(limit - tg, 1e-6) };
  return { band: "hot", f: Math.max(0, Math.min(1, (c - limit) / Math.max(hi - limit, 1e-6))) };
}
