// Problems & fixes, the logic without the page (T-561, plan §13): findings grouped by impact, the fixes under each
// one (slicer settings, design changes, process tips, CAD advice), the one pending batch with its conflicts, what a
// confirmed batch does to the chosen option, which problems a change brought in, and holds / loads carried over to
// a changed model. No DOM: pure logic, unit-tested under Node (core/tests/test_problems_step.py).

/** Impact groups in the order the list shows them (checks.py RULE_IMPACT). */
export const GROUPS = ["fails", "shape", "surface", "weak", "info"];
const SEVERITY = { problem: "bad", warn: "warn", info: "info" };

/** [{impact, findings}] for the groups that have any, worst first. */
export function groupFindings(findings) {
  const by = new Map(GROUPS.map((g) => [g, []]));
  for (const f of findings) by.get(GROUPS.includes(f.impact) ? f.impact : "info").push(f);
  return GROUPS.map((impact) => ({ impact, findings: by.get(impact) })).filter((g) => g.findings.length);
}

export const severityOf = (finding) => SEVERITY[finding.severity] ?? "info";
const findingOf = (x) => x.finding_id ?? x.fixes?.finding_id ?? null;
const designFix = (edit, findingId, modelLocked) => ({
  id: `edit:${findingId}:${edit.id}`, tag: "design", text: edit.title, editIds: [edit.id], changesModel: modelLocked,
});

/** Design edits that the engine offers without a finding. */
export function optionalImprovements(design, { modelLocked = false } = {}) {
  return (design?.edits ?? []).filter((e) => e.source === "other" && !e.clash)
    .map((e) => designFix(e, "other", modelLocked));
}

/**
 * The fixes under one finding, fixes first in the order Slicer, Design, Process (§13). ``design`` is api.design's
 * result (edits, advice) or null. Edits for one finding with the same title (a through hole is two hole ends) become
 * one fix. ``modelLocked``: the user allowed slicer settings only, so a design fix is tagged "changes your model".
 * Each fix: {id, tag: slicer | design | process, text, settings?, editIds?, cad?, steps?, changesModel?}.
 */
export function fixesFor(finding, design, { modelLocked = false } = {}) {
  const fid = finding.finding_id;
  const out = [];
  (finding.fixes ?? []).forEach((fx, i) => {
    if (fx.domain === "slicer" && fx.settings && Object.keys(fx.settings).length) out.push({ id: `set:${fid}:${i}`, tag: "slicer", text: fx.text, settings: fx.settings });
  });
  const edits = new Map();
  for (const e of design?.edits ?? []) {
    if (findingOf(e) !== fid || e.clash) continue;
    const fix = edits.get(e.title);
    if (fix) fix.editIds.push(e.id);
    else edits.set(e.title, designFix(e, fid, modelLocked));
  }
  out.push(...edits.values());
  for (const a of design?.advice ?? []) {
    const ids = a.fixes?.finding_ids ?? (a.fixes?.finding_id ? [a.fixes.finding_id] : []);
    if (a.edit_id || !ids.includes(fid)) continue;
    out.push({ id: `cad:${fid}:${a.id}`, tag: "design", text: a.text, cad: true, steps: true });
  }
  (finding.fixes ?? []).forEach((fx, i) => {
    if (fx.domain === "process") out.push({ id: `proc:${fid}:${i}`, tag: "process", text: fx.text, steps: true });
  });
  return out;
}

/** What a fix puts into the pending batch: a design change (edit ids) or settings with their old and new values. */
export function pendingItem(fix, finding, option) {
  if (fix.tag === "design") return { id: fix.id, kind: "design", findingId: finding?.finding_id ?? null, title: fix.text, editIds: fix.editIds };
  const changes = Object.entries(fix.settings).map(([key, to]) => ({ key, from: option?.settings?.[key] ?? null, to: String(to) }));
  return { id: fix.id, kind: "setting", findingId: finding.finding_id, title: fix.text, changes };
}

/** Settings that two pending fixes want set differently: [{key, values: [..], ids: [..]}]. They must be resolved first. */
export function conflicts(pending) {
  const by = new Map();
  for (const p of pending) {
    if (p.kind !== "setting") continue;
    for (const c of p.changes) {
      const v = by.get(c.key) ?? { key: c.key, values: [], ids: [] };
      if (!v.values.includes(c.to)) v.values.push(c.to);
      v.ids.push(p.id);
      by.set(c.key, v);
    }
  }
  return [...by.values()].filter((v) => v.values.length > 1);
}

// Slicer keys that are also the option's own numbers (what strength uses).
const NUMBERS = {
  wall_loops: (v) => ({ walls: parseInt(v, 10) }),
  top_shell_layers: (v) => ({ top: parseInt(v, 10) }),
  bottom_shell_layers: (v) => ({ bottom: parseInt(v, 10) }),
  sparse_infill_density: (v) => ({ infill: parseFloat(v) / 100 }),
};

/** The option with the batch's settings in it (and walls, infill, top, bottom kept in step), plus the fixes it carries. */
export function applySettings(option, pending) {
  const settings = { ...(option.settings ?? {}) };
  let numbers = {};
  const fixed = new Set(option.fixed ?? []);
  for (const p of pending) {
    if (p.kind !== "setting") continue;
    for (const c of p.changes) {
      settings[c.key] = c.to;
      if (NUMBERS[c.key]) numbers = { ...numbers, ...NUMBERS[c.key](c.to) };
    }
    fixed.add(p.findingId);
  }
  return { ...option, ...numbers, settings, fixed: [...fixed] };
}

/** A finding's status in the list: accepted, new (a change brought it in), fixed (its slicer fix is in the
 * option), pending (a fix waits in the batch) or open. */
export function statusOf(finding, { option, accepted = [], newIds = [], pending = [] }) {
  const id = finding.finding_id;
  if (accepted.includes(id)) return "accepted";
  if ((option?.fixed ?? []).includes(id)) return "fixed";
  if (newIds.includes(id)) return "new";
  if (pending.some((p) => p.findingId === id)) return "pending";
  return "open";
}

/** Problems still to look at: not info, not fixed or accepted. */
export const openProblems = (findings, ctx) => findings.filter((f) => f.severity !== "info" && !["fixed", "accepted"].includes(statusOf(f, ctx)));

/**
 * Which findings after a change are new: per rule, findings beyond the count before, taking those whose id wasn't
 * there (finding ids carry the place, so an unchanged problem keeps its id). Returns their ids.
 */
export function newProblems(before, after) {
  const was = new Set(before.map((f) => f.finding_id));
  const count = (list) => list.reduce((m, f) => m.set(f.rule, (m.get(f.rule) ?? 0) + (f.severity === "info" ? 0 : 1)), new Map());
  const n0 = count(before), n1 = count(after);
  const out = [];
  for (const [rule, n] of n1) {
    let extra = n - (n0.get(rule) ?? 0);
    for (const f of after) {
      if (extra <= 0) break;
      if (f.rule === rule && f.severity !== "info" && !was.has(f.finding_id)) { out.push(f.finding_id); extra -= 1; }
    }
  }
  return out;
}

/** How many problems a batch fixed: the ones gone after the change (rules' counts that went down, info left out),
 * plus the ones still found that a confirmed slicer setting now takes care of (the check can't see settings). */
export function fixedCount(before, after, pending = []) {
  const count = (list) => list.reduce((m, f) => (f.severity === "info" ? m : m.set(f.rule, (m.get(f.rule) ?? 0) + 1)), new Map());
  const n0 = count(before), n1 = count(after);
  let fixed = 0;
  for (const [rule, n] of n0) fixed += Math.max(0, n - (n1.get(rule) ?? 0));
  const still = new Set(after.map((f) => f.finding_id));
  const bySetting = new Set(pending.filter((p) => p.kind === "setting" && still.has(p.findingId)).map((p) => p.findingId));
  return fixed + bySetting.size;
}

const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const dist = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]);

/** The face of the new model that faces the same way nearest the old face's middle, or -1 (as the old app did). */
function sameFace(old, regions) {
  let best = -1, bestD = Infinity;
  for (const r of regions) {
    const d = dist(r.centroid, old.centroid);
    if (dot(r.normal, old.normal) > 0.9 && d < bestD) [best, bestD] = [r.id, d];
  }
  return best;
}

/** The edge of the new model nearest the old edge's midpoint, facing about the same way, or -1. */
function sameEdge(old, edges) {
  let best = -1, bestD = Infinity;
  for (const e of edges) {
    const d = dist(e.midpoint, old.midpoint);
    if (dot(e.normal, old.normal) > 0.7 && d < bestD && d < 2 + 0.5 * (old.length_mm ?? 0)) [best, bestD] = [e.id, d];
  }
  return best;
}

/**
 * Holds and loads moved onto a changed model: ``before`` and ``after`` are api.regions results. Faces go to the face
 * facing the same way nearest the old one; edges to the nearest edge of the same kind. Ones whose face or edge is
 * gone are dropped and counted in ``lost``.
 */
export function carryOver(items, before, after) {
  const edgesOf = (r, kind) => (kind === "concave" ? r.concave_edges : r.feature_edges)?.meta ?? [];
  let lost = 0;
  const kept = items.flatMap((it) => {
    if (it.edge != null) {
      const old = edgesOf(before, it.edgeKind).find((e) => e.id === it.edge);
      const edge = old ? sameEdge(old, edgesOf(after, it.edgeKind)) : -1;
      if (edge < 0) { lost += 1; return []; }
      const face = before.regions[it.region];
      const region = face ? sameFace(face, after.regions) : -1;
      return [{ ...it, edge, region: region >= 0 ? region : it.region }];
    }
    const old = before.regions[it.region];
    const region = old ? sameFace(old, after.regions) : -1;
    if (region < 0) { lost += 1; return []; }
    return [{ ...it, region }];
  });
  return { items: kept, lost };
}

/** A box in the print frame (check's bbox: turned by ``rotation``, centred on the bed, resting on it) back in part
 * coordinates. ``lo``/``hi``: the part's turned bounds, which fix the centring. Returns the 8 corners. */
export function boxToPart(bbox, rotation, turnedLo, turnedHi) {
  const base = [(turnedLo[0] + turnedHi[0]) / 2, (turnedLo[1] + turnedHi[1]) / 2, turnedLo[2]];
  const [a, b] = bbox;
  const corners = [];
  for (const x of [a[0], b[0]]) for (const y of [a[1], b[1]]) for (const z of [a[2], b[2]]) {
    const p = [x + base[0], y + base[1], z + base[2]];
    // part = Rᵀ · turned (rotation is orthonormal)
    corners.push([0, 1, 2].map((i) => rotation[0][i] * p[0] + rotation[1][i] * p[1] + rotation[2][i] * p[2]));
  }
  return corners;
}

/** The turned bounds of a triangle soup (Float32Array, part coordinates) under ``rotation``. */
export function turnedBounds(soup, rotation) {
  const lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity];
  for (let i = 0; i < soup.length; i += 3) {
    for (let k = 0; k < 3; k++) {
      const v = rotation[k][0] * soup[i] + rotation[k][1] * soup[i + 1] + rotation[k][2] * soup[i + 2];
      if (v < lo[k]) lo[k] = v;
      if (v > hi[k]) hi[k] = v;
    }
  }
  return [lo, hi];
}
