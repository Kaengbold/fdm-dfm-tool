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
 * Do the importer's face ids name the same faces as OpenCascade's order on ``solid``? Each face is compared by area
 * and centre. The triangles only approximate curved faces, so a face passes when it is close (``areaTol`` relative;
 * the centre within ``mm`` or ``share`` of the face's size) and no other face of the solid fits it better. Returns
 * {ok, faces, worst: {id, area, mm}, misfit}; an edit worker falls back to the mesh edit when it isn't ok.
 */
export function checkFaceOrder(oc, solid, soup, faceIds, { areaTol = 0.05, mm = 0.25, share = 0.02 } = {}) {
  const ref = faces(oc, solid).map((f) => faceSignature(oc, f));
  const got = meshSignatures(soup, faceIds);
  const worst = { id: -1, area: 0, mm: 0 }; // the largest misfits (relative area, centre distance) and the worst face
  const fit = (g, r) => [Math.abs(g.area - r.area) / Math.max(r.area, 1e-9), Math.hypot(...g.centre.map((v, i) => v - r.centre[i]))];
  let ok = ref.length === got.length, score = 0, misfit = null;
  got.forEach((g, id) => {
    if (!ref[id]) { ok = false; return; }
    const [area, dist] = fit(g, ref[id]);
    worst.area = Math.max(worst.area, area);
    worst.mm = Math.max(worst.mm, dist);
    const s = Math.max(area / areaTol, dist / Math.max(mm, share * Math.sqrt(ref[id].area)));
    if (s > score) { score = s; worst.id = id; }
    const mine = area + dist / Math.sqrt(ref[id].area);
    const other = ref.findIndex((r, j) => j !== id && (([a, d]) => a + d / Math.sqrt(r.area))(fit(g, r)) < mine * 0.5);
    if (other >= 0 && misfit === null) misfit = { id, fits: other };
  });
  return { ok: ok && score <= 1 && misfit === null, faces: ref.length, worst, misfit };
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

/** What kind of surface each face is ("plane", "cylinder", "torus", …), with the radius of cylinders. */
export function surfaces(oc, shape) {
  const names = Object.fromEntries(Object.entries(oc.GeomAbs_SurfaceType).map(([k, v]) => [v, k.replace("GeomAbs_", "").toLowerCase()]));
  return faces(oc, shape).map((f) => {
    const a = new oc.BRepAdaptor_Surface(f, true);
    const kind = names[a.GetType()] ?? "other";
    return kind === "cylinder" ? { kind, radius: a.Cylinder().Radius() } : { kind };
  });
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

// ---- B-rep design edits (T-529) ----
// Edits come from the engine with an ``intent`` (features.py, T-528): exact shapes (flat outlines pushed along a
// vector, rings round an axis) or rounds / chamfers on the edge between two B-rep faces. Each is made with exact
// OpenCascade tools and checked (BRepCheck_Analyzer). One that fails falls back to its triangle pieces sewn into a
// solid (the old path: right shape, faceted); one that fails that way too is left out. A note says which.

const progress = (oc) => new oc.Message_ProgressRange();
const pnt = (oc, p) => new oc.gp_Pnt(p[0], p[1], p[2]);

export function volume(oc, shape) {
  const props = new oc.GProp_GProps();
  oc.BRepGProp.VolumeProperties(shape, props, false, false, false);
  const v = props.Mass();
  props.delete();
  return v;
}

export const isValid = (oc, shape) => Boolean(shape) && !shape.IsNull() && new oc.BRepCheck_Analyzer(shape, true, false).IsValid();

/** A closed triangle soup (Float32Array, 9 floats per triangle) as an OpenCascade solid. */
export function soupToSolid(oc, soup) {
  const sew = new oc.BRepBuilderAPI_Sewing(1e-4, true, true, true, false);
  const P = (i) => new oc.gp_Pnt(soup[i], soup[i + 1], soup[i + 2]);
  for (let t = 0; t < soup.length; t += 9) {
    const [a, b, c] = [P(t), P(t + 3), P(t + 6)];
    if (a.Distance(b) < 1e-6 || b.Distance(c) < 1e-6 || c.Distance(a) < 1e-6) continue;
    const edge = (p, q) => new oc.BRepBuilderAPI_MakeEdge(p, q).Edge();
    const wire = new oc.BRepBuilderAPI_MakeWire(edge(a, b), edge(b, c), edge(c, a)).Wire();
    sew.Add(new oc.BRepBuilderAPI_MakeFace(wire, true).Face());
  }
  sew.Perform(progress(oc));
  let solid = new oc.BRepBuilderAPI_MakeSolid(oc.TopoDS.Shell(sew.SewedShape())).Solid();
  if (volume(oc, solid) < 0) solid = oc.TopoDS.Solid(solid.Reversed());
  sew.delete();
  return solid;
}

function polygonWire(oc, pts) {
  const w = new oc.BRepBuilderAPI_MakeWire();
  for (let i = 0; i < pts.length; i++) {
    const a = pts[i], b = pts[(i + 1) % pts.length];
    if (Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]) > 1e-6) w.Add(new oc.BRepBuilderAPI_MakeEdge(pnt(oc, a), pnt(oc, b)).Edge());
  }
  if (!w.IsDone()) throw new Error("open outline");
  return w.Wire();
}

/** An intent shape as an exact solid: a flat outline pushed along a vector, or a ring round an axis. */
export function shapeSolid(oc, s) {
  if (s.kind === "prism") {
    const face = new oc.BRepBuilderAPI_MakeFace(polygonWire(oc, s.outline), true);
    for (const hole of s.holes ?? []) face.Add(polygonWire(oc, hole));
    if (!face.IsDone()) throw new Error("the outline isn't flat");
    return new oc.BRepPrimAPI_MakePrism(face.Face(), new oc.gp_Vec(...s.extrude), false, true).Shape();
  }
  if (s.kind === "ring") {
    const n = Math.hypot(...s.axis), axis = s.axis.map((v) => v / n);
    const at = new oc.gp_Ax2(pnt(oc, s.center.map((c, i) => c + axis[i] * s.from)), new oc.gp_Dir(...axis));
    const height = s.to - s.from;
    let solid = new oc.BRepPrimAPI_MakeCylinder(at, s.outer, height).Shape();
    if (s.inner > 0) solid = new oc.BRepAlgoAPI_Cut(solid, new oc.BRepPrimAPI_MakeCylinder(at, s.inner, height).Shape(), progress(oc)).Shape();
    return solid;
  }
  throw new Error(`unknown shape ${s.kind}`);
}

/** The B-rep edges an inside-corner intent names, on the solid as read (its face ids are the importer's). */
function cornerEdges(oc, solid, intent) {
  const out = { fillet: [], chamfer: [] };
  for (const item of intent.refs?.edges ?? []) {
    if (!item.faces) throw new Error("this corner has no CAD faces");
    const edges = sharedEdges(oc, solid, item.faces[0], item.faces[1]);
    if (!edges.length) throw new Error("these faces share no edge");
    for (const edge of edges) out[item.op === "chamfer" ? "chamfer" : "fillet"].push({ edge, size: item.size });
  }
  return out;
}

/** ``shape`` with rounds or chamfers on ``items`` ([{edge, size}]): {shape, moved} where ``moved(edge)`` is that edge
 * of ``shape`` as it is in the result (a round trims the edges next to it, so they are new edges there). */
function blend(oc, shape, kind, items, scale = 1) {
  if (!items.length) return { shape, moved: (e) => e };
  const mk = kind === "fillet" ? new oc.BRepFilletAPI_MakeFillet(shape, oc.ChFi3d_FilletShape.ChFi3d_Rational) : new oc.BRepFilletAPI_MakeChamfer(shape);
  for (const { edge, size } of items) mk.Add(size * scale, edge);
  try {
    mk.Build(progress(oc));
  } catch {
    // an OpenCascade exception: treated as not done
  }
  if (!mk.IsDone()) throw new Error(kind === "fillet" ? "OpenCascade could not make the round" : "OpenCascade could not make the chamfer");
  const moved = (e) => {
    const list = mk.Modified(e);
    return list.Size() ? oc.TopoDS.Edge(list.First()) : e;
  };
  return { shape: mk.Shape(), moved };
}

// A round or chamfer wider than the faces beside its edge can't be made; real parts often have narrow faces there
// (T-535: most failed at 3 mm, all worked at 1 mm). Try smaller before giving up, and say so.
const BLEND_SCALES = [1, 0.5, 0.25];

/** Rounds then chamfers on ``shape``, at the largest of BLEND_SCALES that gives a valid solid: {shape, scale}. */
function blendCorner(oc, shape, { fillet, chamfer }) {
  let err;
  for (const scale of BLEND_SCALES) {
    try {
      const round = blend(oc, shape, "fillet", fillet, scale);
      const next = blend(oc, round.shape, "chamfer", chamfer.map((c) => ({ ...c, edge: round.moved(c.edge) })), scale).shape;
      if (!isValid(oc, next)) throw new Error("the result wasn't a valid solid");
      return { shape: next, scale };
    } catch (e) {
      err = e;
    }
  }
  throw err;
}

function boolean(oc, shape, tool, add) {
  const b = add ? new oc.BRepAlgoAPI_Fuse(shape, tool, progress(oc)) : new oc.BRepAlgoAPI_Cut(shape, tool, progress(oc));
  if (!b.IsDone()) throw new Error("the boolean failed");
  return b.Shape();
}

/** The edit's triangle pieces of one kind ("add" / "cut"), each sewn into a solid. */
function pieceSolids(oc, edit, op) {
  const out = [];
  let start = 0;
  for (const [i, n] of edit.pieces.entries()) {
    const soup = edit.mesh.subarray(start * 9, (start + n) * 9);
    start += n;
    if ((edit.piece_ops?.[i] ?? edit.op) === op) out.push(soupToSolid(oc, soup));
  }
  return out;
}

const CORNER_OPS = ["fillet", "chamfer"];
// Each faceted piece is one boolean on an ever bigger solid: a round along a curved corner comes as dozens of
// pieces, took a minute and 1.8 GB on a small knob and still came out wrong (T-535). Leave such a change out instead.
const MAX_FACET_PIECES = 8;

/**
 * ``edits`` ([{id, title, op, mesh, pieces, piece_ops?, intent?}], part coordinates) applied to ``solid``: rounds
 * and chamfers first (they name faces of the solid as read), then every addition, then every cut. Returns
 * {shape, notes: [{id, title, how: "exact" | "mesh" | "skipped", why?, scale?}]} (``scale``: a round or chamfer made
 * smaller than planned, as a fraction). ``exact: false`` forces the mesh path.
 */
export function applyEdits(oc, solid, edits, { exact = true } = {}) {
  const notes = new Map(edits.map((e) => [e, { id: e.id, title: e.title, how: exact && e.intent ? "exact" : "mesh" }]));
  for (const e of edits) if (!e.intent) notes.get(e).why = "no exact description of this change";
  const toMesh = (e, err) => Object.assign(notes.get(e), { how: "mesh", why: err?.message ?? String(err) });
  let shape = solid;
  for (const e of edits) {
    if (notes.get(e).how !== "exact" || !CORNER_OPS.includes(e.intent.op)) continue;
    try {
      const made = blendCorner(oc, shape, cornerEdges(oc, solid, e.intent));
      shape = made.shape;
      if (made.scale < 1) notes.get(e).scale = made.scale;
    } catch (err) {
      toMesh(e, err);
    }
  }
  for (const op of ["add", "cut"]) {
    for (const e of edits) {
      const note = notes.get(e);
      if (note.how === "exact" && CORNER_OPS.includes(e.intent.op)) continue; // done above
      if (note.how === "exact") {
        try {
          if (!e.intent.shapes?.length) throw new Error("no exact shape for this change");
          let next = shape;
          for (const s of e.intent.shapes) if (Boolean(s.add) === (op === "add")) next = boolean(oc, next, shapeSolid(oc, s), op === "add");
          if (!isValid(oc, next)) throw new Error("the result wasn't a valid solid");
          shape = next;
          continue;
        } catch (err) {
          toMesh(e, err);
        }
      }
      if (note.how === "skipped") continue;
      try {
        const ops = e.pieces.filter((_, i) => (e.piece_ops?.[i] ?? e.op) === op).length;
        if (ops > MAX_FACET_PIECES) throw new Error("too many faceted pieces to add to the CAD part");
        let next = shape;
        for (const piece of pieceSolids(oc, e, op)) next = boolean(oc, next, piece, op === "add");
        if (!isValid(oc, next)) throw new Error("the faceted change wasn't a valid solid either");
        const before = volume(oc, shape);
        if (ops && (volume(oc, next) - before) * (op === "add" ? 1 : -1) < -1e-6 * before) throw new Error("the faceted change came out the wrong way round");
        shape = next;
      } catch (err) {
        Object.assign(note, { how: "skipped", why: [note.why, err?.message ?? String(err)].filter(Boolean).join("; ") });
      }
    }
  }
  // Merge faces the edits split (a slab's top and the part's); keep the unmerged solid if merging breaks it.
  const unify = new oc.ShapeUpgrade_UnifySameDomain(shape, true, true, false);
  unify.Build();
  const merged = unify.Shape();
  return { shape: isValid(oc, merged) ? merged : shape, notes: [...notes.values()] };
}

/** Triangles of a shape with the face id (explorer order) of each, for display and the engine's re-check. */
export function tessellate(oc, shape, deflection = 0.05) {
  new oc.BRepMesh_IncrementalMesh(shape, deflection, false, 0.3, false);
  const soup = [], ids = [];
  faces(oc, shape).forEach((face, id) => {
    const loc = new oc.TopLoc_Location();
    const tri = oc.BRep_Tool.Triangulation(face, loc, 0);
    if (!tri || (tri.IsNull && tri.IsNull())) return;
    const T = tri.get ? tri.get() : tri;
    const trsf = loc.Transformation();
    const flip = face.Orientation() !== oc.TopAbs_Orientation.TopAbs_FORWARD;
    const node = (k) => { const p = T.Node(k).Transformed(trsf); return [p.X(), p.Y(), p.Z()]; };
    for (let k = 1; k <= T.NbTriangles(); k++) {
      const t = T.Triangle(k);
      const v = [t.Value(1), t.Value(2), t.Value(3)];
      if (flip) v.reverse();
      for (const i of v) soup.push(...node(i));
      ids.push(id);
    }
  });
  return { soup: new Float32Array(soup), faceIds: new Uint32Array(ids) };
}
