// B-rep helpers for design edits on STEP parts (T-527, research/cad_engine.md): the link between what the engine
// knows (a CAD face id per triangle from the STEP importer, web/step-worker.js) and OpenCascade's shapes, which the
// edit worker changes. Every function takes the OpenCascade module ``oc`` (replicad's build, as stepwrite-worker.js
// loads it), so the same code runs in the worker and under Node (tools/check_step_faces.mjs).
//
// Face ids: occt-import-js numbers the faces of each body in OpenCascade's own TopExp_Explorer order, from 0 per
// body; the bodies come in the file's order, one per solid. Checked by check_step_faces.mjs on every test STEP.

const FACE = (oc) => oc.TopAbs_ShapeEnum.TopAbs_FACE;
const EDGE = (oc) => oc.TopAbs_ShapeEnum.TopAbs_EDGE;
const SHAPE = (oc) => oc.TopAbs_ShapeEnum.TopAbs_SHAPE;

function collect(oc, shape, kind, cast) {
  const out = [];
  for (const ex = new oc.TopExp_Explorer(shape, kind, SHAPE(oc)); ex.More(); ex.Next()) out.push(cast(ex.Current()));
  return out;
}

/** The solids of a shape (a STEP file's bodies), in the file's order. */
export const solids = (oc, shape) => collect(oc, shape, oc.TopAbs_ShapeEnum.TopAbs_SOLID, (s) => oc.TopoDS.Solid(s));

/** The faces of a solid; index = the importer's face id for that body. */
export const faces = (oc, solid) => collect(oc, solid, FACE(oc), (s) => oc.TopoDS.Face(s));

/** A face's area (mm²) and centre, to tell faces apart. */
export function faceSignature(oc, face) {
  const p = new oc.GProp_GProps();
  oc.BRepGProp.SurfaceProperties(face, p, false, false);
  const c = p.CentreOfMass();
  const out = { area: p.Mass(), centre: [c.X(), c.Y(), c.Z()] };
  p.delete();
  return out;
}

/** The same per face id from triangles: ``soup`` (9 floats per triangle) and ``faceIds`` (one per triangle). */
export function meshSignatures(soup, faceIds) {
  const n = faceIds.reduce((m, f) => Math.max(m, f + 1), 0);
  const out = Array.from({ length: n }, () => ({ area: 0, centre: [0, 0, 0] }));
  for (let t = 0; t < faceIds.length; t++) {
    const o = t * 9;
    const u = [soup[o + 3] - soup[o], soup[o + 4] - soup[o + 1], soup[o + 5] - soup[o + 2]];
    const w = [soup[o + 6] - soup[o], soup[o + 7] - soup[o + 1], soup[o + 8] - soup[o + 2]];
    const a = Math.hypot(u[1] * w[2] - u[2] * w[1], u[2] * w[0] - u[0] * w[2], u[0] * w[1] - u[1] * w[0]) / 2;
    const f = out[faceIds[t]];
    f.area += a;
    for (let i = 0; i < 3; i++) f.centre[i] += (a * (soup[o + i] + soup[o + 3 + i] + soup[o + 6 + i])) / 3;
  }
  for (const f of out) if (f.area > 0) f.centre = f.centre.map((v) => v / f.area);
  return out;
}

/**
 * Do the importer's face ids name the same faces as OpenCascade's order on ``solid``? Compared by area and centre
 * (the triangles only approximate curved faces: ``areaTol`` relative, ``mm`` for the centre). Returns {ok, faces,
 * worst: {id, area, mm}}; an edit worker falls back to the mesh edit when it isn't ok.
 */
export function checkFaceOrder(oc, solid, soup, faceIds, { areaTol = 0.02, mm = 0.25 } = {}) {
  const ref = faces(oc, solid).map((f) => faceSignature(oc, f));
  const got = meshSignatures(soup, faceIds);
  const worst = { id: -1, area: 0, mm: 0 }; // the largest misfits (relative area, centre distance) and the worst face
  let ok = ref.length === got.length, score = 0;
  got.forEach((g, id) => {
    const r = ref[id];
    if (!r) { ok = false; return; }
    const area = Math.abs(g.area - r.area) / Math.max(r.area, 1e-9);
    const dist = Math.hypot(...g.centre.map((v, i) => v - r.centre[i]));
    worst.area = Math.max(worst.area, area);
    worst.mm = Math.max(worst.mm, dist);
    if (Math.max(area / areaTol, dist / mm) > score) { score = Math.max(area / areaTol, dist / mm); worst.id = id; }
  });
  return { ok: ok && score <= 1, faces: ref.length, worst };
}

/** The B-rep edges two faces share (an inside corner between faces ``a`` and ``b`` of ``solid``); [] if they don't
 * touch. A curved corner can be split into several edges. */
export function sharedEdges(oc, solid, a, b) {
  const all = faces(oc, solid);
  if (!all[a] || !all[b] || a === b) return [];
  const mine = collect(oc, all[a], EDGE(oc), (s) => oc.TopoDS.Edge(s));
  const theirs = collect(oc, all[b], EDGE(oc), (s) => oc.TopoDS.Edge(s));
  const out = [];
  for (const e of mine) if (theirs.some((f) => f.IsSame(e)) && !out.some((f) => f.IsSame(e))) out.push(e);
  return out;
}

/** An edge's length (mm) and centre, for checks and notes. */
export function edgeSignature(oc, edge) {
  const p = new oc.GProp_GProps();
  oc.BRepGProp.LinearProperties(edge, p, false, false);
  const c = p.CentreOfMass();
  const out = { length: p.Mass(), centre: [c.X(), c.Y(), c.Z()] };
  p.delete();
  return out;
}
