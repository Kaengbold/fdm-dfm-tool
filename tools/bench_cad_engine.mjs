// Research benchmark for T-526 (research/cad_engine.md), not product code: times the design edits the new UI
// needs as B-rep operations with OpenCascade (replicad's WASM build, the one stepwrite-worker.js already loads)
// against the mesh path (manifold-3d), on tools/fixtures/l_bracket.step. Dev-only:
//   npm install --no-save --no-package-lock --prefix tools pyodide@0.27.2 replicad-opencascadejs@1.1.0 manifold-3d@3.5.4 ws
//   node tools/bench_cad_engine.mjs
// (install all four in one command: npm --no-save prunes packages it isn't told about.)
import Module from "replicad-opencascadejs";
import fs from "node:fs";
import path from "node:path";
import zlib from "node:zlib";
import { performance } from "node:perf_hooks";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const ms = (t) => `${(performance.now() - t).toFixed(0)} ms`;
const size = (f) => {
  const b = fs.readFileSync(path.join(here, "node_modules", f));
  return `${(b.length / 1e6).toFixed(1)} MB (${(zlib.brotliCompressSync(b).length / 1e6).toFixed(1)} MB brotli)`;
};
console.log("OpenCascade wasm:", size("replicad-opencascadejs/dist/replicad_single.wasm"));
console.log("manifold wasm:   ", size("manifold-3d/manifold.wasm"));

let t = performance.now();
const oc = await Module();
console.log("OpenCascade start-up:", ms(t));
const range = () => new oc.Message_ProgressRange();
const count = (shape, kind) => { let n = 0; for (const ex = new oc.TopExp_Explorer(shape, kind, oc.TopAbs_ShapeEnum.TopAbs_SHAPE); ex.More(); ex.Next()) n++; return n; };
const faces = (s) => count(s, oc.TopAbs_ShapeEnum.TopAbs_FACE);
const volume = (s) => { const p = new oc.GProp_GProps(); oc.BRepGProp.VolumeProperties(s, p, false, false, false); return p.Mass(); };
const centre = (e) => { const p = new oc.GProp_GProps(); oc.BRepGProp.LinearProperties(e, p, false, false); const c = p.CentreOfMass(); return [c.X(), c.Y(), c.Z()]; };
const box = (x, y, z, dx, dy, dz) => new oc.BRepPrimAPI_MakeBox(new oc.gp_Pnt(x, y, z), dx, dy, dz).Shape();
const cyl = (p, d, r, h) => new oc.BRepPrimAPI_MakeCylinder(new oc.gp_Ax2(new oc.gp_Pnt(...p), new oc.gp_Dir(...d)), r, h).Shape();
const fuse = (a, b) => new oc.BRepAlgoAPI_Fuse(a, b, range()).Shape();
const cut = (a, b) => new oc.BRepAlgoAPI_Cut(a, b, range()).Shape();
// A closed triangle as a wire (this build has no BRepBuilderAPI_MakePolygon; same as stepwrite-worker.js).
const triWire = (a, b, c) => {
  const e = (p, q) => new oc.BRepBuilderAPI_MakeEdge(p, q).Edge();
  return new oc.BRepBuilderAPI_MakeWire(e(a, b), e(b, c), e(c, a)).Wire();
};
const unify = (s) => { const u = new oc.ShapeUpgrade_UnifySameDomain(s, true, true, false); u.Build(); return u.Shape(); };

t = performance.now();
oc.FS.writeFile("/in.step", fs.readFileSync(path.join(here, "fixtures", "l_bracket.step")));
const reader = new oc.STEPControl_Reader();
reader.ReadFile("/in.step");
reader.TransferRoots(range());
const part = reader.OneShape();
console.log(`STEP read: ${ms(t)}, ${faces(part)} faces, ${volume(part).toFixed(0)} mm³`);

const results = [];
function bench(name, fn) {
  const t0 = performance.now();
  let shape, err;
  try { shape = fn(); } catch (e) { err = e?.message ?? String(e); }
  const time = performance.now() - t0;
  const ok = shape && !shape.IsNull() && new oc.BRepCheck_Analyzer(shape, true, false).IsValid();
  results.push({ name, ms: Math.round(time), ok: Boolean(ok), faces: ok ? faces(shape) : null, dV: ok ? Math.round(volume(shape) - volume(part)) : null, err });
  return ok ? shape : null;
}

// The inside corner where the base (z 0..5) meets the upright (x -30..-25): the edge at x = -25, z = 5.
const edges = [];
for (const ex = new oc.TopExp_Explorer(part, oc.TopAbs_ShapeEnum.TopAbs_EDGE, oc.TopAbs_ShapeEnum.TopAbs_SHAPE); ex.More(); ex.Next()) edges.push(oc.TopoDS.Edge(ex.Current()));
const corner = edges.find((e) => { const [x, , z] = centre(e); return Math.abs(x + 25) < 0.01 && Math.abs(z - 5) < 0.01; });

bench("fillet inside corner r3 (B-rep)", () => {
  const f = new oc.BRepFilletAPI_MakeFillet(part, oc.ChFi3d_FilletShape.ChFi3d_Rational);
  f.Add(3, corner);
  f.Build(range());
  return f.Shape();
});
bench("chamfer inside corner 3 mm (B-rep)", () => {
  const f = new oc.BRepFilletAPI_MakeChamfer(part);
  f.Add(3, corner);
  f.Build(range());
  return f.Shape();
});
bench("thicken whole part +0.5 mm (offset)", () => {
  const o = new oc.BRepOffsetAPI_MakeOffsetShape();
  o.PerformByJoin(part, 0.5, 1e-3, oc.BRepOffset_Mode.BRepOffset_Skin, false, false, oc.GeomAbs_JoinType.GeomAbs_Arc, false, range());
  return o.Shape();
});
bench("thicken base locally (+2 mm slab, fuse)", () => unify(fuse(part, box(-25, -15, 5, 40, 30, 2))));
bench("hidden void (box inside the base, cut)", () => unify(cut(part, box(-10, -10, 1.2, 30, 20, 2.6))));
bench("rib / gusset (prism, fuse)", () => {
  const P = (x, y, z) => new oc.gp_Pnt(x, y, z);
  const tri = triWire(P(-25, -1, 5), P(-10, -1, 5), P(-25, -1, 20));
  const face = new oc.BRepBuilderAPI_MakeFace(tri, true).Face();
  const prism = new oc.BRepPrimAPI_MakePrism(face, new oc.gp_Vec(0, 2, 0), false, true).Shape();
  return unify(fuse(part, prism));
});
bench("upright hole Ø6 → Ø6.4 (cut bigger cylinder)", () => unify(cut(part, cyl([10, 0, -1], [0, 0, 1], 3.2, 7))));
bench("teardrop top on sideways Ø8 hole (cut prism)", () => {
  const P = (x, y, z) => new oc.gp_Pnt(x, y, z);
  const r = 4, apex = r * Math.SQRT2;
  const w = triWire(P(-31, -r * Math.SQRT1_2, 28 + r * Math.SQRT1_2), P(-31, 0, 28 + apex), P(-31, r * Math.SQRT1_2, 28 + r * Math.SQRT1_2));
  const prism = new oc.BRepPrimAPI_MakePrism(new oc.BRepBuilderAPI_MakeFace(w, true).Face(), new oc.gp_Vec(8, 0, 0), false, true).Shape();
  return unify(cut(part, prism));
});
const filleted = results[0].ok ? (() => { const f = new oc.BRepFilletAPI_MakeFillet(part, oc.ChFi3d_FilletShape.ChFi3d_Rational); f.Add(3, corner); f.Build(range()); return f.Shape(); })() : part;
t = performance.now();
const writer = new oc.STEPControl_Writer();
writer.Transfer(filleted, oc.STEPControl_StepModelType.STEPControl_AsIs, true, range());
writer.Write("/out.step");
const stepOut = oc.FS.readFile("/out.step");
console.log(`STEP write (filleted part): ${ms(t)}, ${stepOut.length} bytes`);
t = performance.now();
new oc.BRepMesh_IncrementalMesh(filleted, 0.05, false, 0.3, false);
console.log(`Tessellate for display/checks (0.05 mm): ${ms(t)}`);
console.table(results);

// The mesh path for the same kind of edit: manifold union of a slab on the tessellated bracket.
const { default: ManifoldModule } = await import("manifold-3d");
t = performance.now();
const wasm = await ManifoldModule();
wasm.setup();
console.log("manifold start-up:", ms(t));
const { Manifold } = wasm;
t = performance.now();
const L = Manifold.cube([60, 30, 5]).translate([-30, -15, 0]).add(Manifold.cube([5, 30, 40]).translate([-30, -15, 0]))
  .subtract(Manifold.cylinder(30, 4, 4, 64).rotate([0, 90, 0]).translate([-40, 0, 28]))
  .subtract(Manifold.cylinder(20, 3, 3, 64).translate([10, 0, -5]));
const slab = Manifold.cube([40, 30, 2]).translate([-25, -15, 5]);
const n = 50;
t = performance.now();
for (let i = 0; i < n; i++) L.add(slab).volume();
console.log(`manifold union (bracket + slab): ${((performance.now() - t) / n).toFixed(2)} ms each, ${L.add(slab).numTri()} triangles`);

// Today's STEP write-back (stepwrite-worker.js): an edit arrives as triangles, is sewn into a solid and cut;
// compare the faces it leaves with the exact B-rep cut of the same Ø6.4 hole.
const holeMesh = Manifold.cylinder(7, 3.2, 3.2, 64).translate([10, 0, -1]).getMesh();
const tris = holeMesh.triVerts, pos = holeMesh.vertProperties, np_ = holeMesh.numProp;
const sew = new oc.BRepBuilderAPI_Sewing(1e-4, true, true, true, false);
for (let i = 0; i < tris.length; i += 3) {
  const P = (k) => new oc.gp_Pnt(pos[k * np_], pos[k * np_ + 1], pos[k * np_ + 2]);
  sew.Add(new oc.BRepBuilderAPI_MakeFace(triWire(P(tris[i]), P(tris[i + 1]), P(tris[i + 2])), true).Face());
}
t = performance.now();
sew.Perform(range());
let meshTool = new oc.BRepBuilderAPI_MakeSolid(oc.TopoDS.Shell(sew.SewedShape())).Solid();
if (volume(meshTool) < 0) meshTool = oc.TopoDS.Solid(meshTool.Reversed());
const viaMesh = unify(cut(part, meshTool));
console.log(`today's path (sewn 64-segment cylinder, cut): ${ms(t)}, ${faces(viaMesh)} faces in the STEP (exact B-rep cut: 10)`);
