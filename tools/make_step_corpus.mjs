// Writes the generated STEP parts in core/tests/corpus_step/ (T-527): CAD shapes the L-bracket fixture lacks, to check
// that face ids agree between the STEP importer and OpenCascade (tools/check_step_faces.mjs). Real-world STEP parts
// join them there with T-535. Dev-only:
//   npm install --no-save --no-package-lock --prefix tools replicad-opencascadejs@1.1.0 occt-import-js@0.0.23
//   node tools/make_step_corpus.mjs
import Module from "replicad-opencascadejs";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const oc = await Module();
const range = () => new oc.Message_ProgressRange();
const P = (x, y, z) => new oc.gp_Pnt(x, y, z);
const box = (x, y, z, dx, dy, dz) => new oc.BRepPrimAPI_MakeBox(P(x, y, z), dx, dy, dz).Shape();
const cylinder = (p, dir, r, h) => new oc.BRepPrimAPI_MakeCylinder(new oc.gp_Ax2(P(...p), new oc.gp_Dir(...dir)), r, h).Shape();
const sphere = (p, r) => new oc.BRepPrimAPI_MakeSphere(P(...p), r).Shape();
const fuse = (a, b) => new oc.BRepAlgoAPI_Fuse(a, b, range()).Shape();
const cut = (a, b) => new oc.BRepAlgoAPI_Cut(a, b, range()).Shape();
const unify = (s) => { const u = new oc.ShapeUpgrade_UnifySameDomain(s, true, true, false); u.Build(); return u.Shape(); };

function edges(shape) {
  const out = [];
  for (const ex = new oc.TopExp_Explorer(shape, oc.TopAbs_ShapeEnum.TopAbs_EDGE, oc.TopAbs_ShapeEnum.TopAbs_SHAPE); ex.More(); ex.Next()) out.push(oc.TopoDS.Edge(ex.Current()));
  return out;
}
function centre(edge) {
  const p = new oc.GProp_GProps();
  oc.BRepGProp.LinearProperties(edge, p, false, false);
  const c = p.CentreOfMass();
  return [c.X(), c.Y(), c.Z()];
}
/** Round (fillet) or bevel (chamfer) the edges whose centre ``pick`` accepts. */
function blend(shape, kind, size, pick) {
  const mk = kind === "fillet" ? new oc.BRepFilletAPI_MakeFillet(shape, oc.ChFi3d_FilletShape.ChFi3d_Rational) : new oc.BRepFilletAPI_MakeChamfer(shape);
  let n = 0;
  for (const e of edges(shape)) if (pick(centre(e))) { mk.Add(size, e); n++; }
  if (!n) throw new Error(`no edge for the ${kind}`);
  mk.Build(range());
  return mk.Shape();
}

// A knob: a chamfered cylinder (a cone face), a sphere cap filleted into it (torus), an axial hole.
let knob = cylinder([0, 0, 0], [0, 0, 1], 15, 10);
knob = blend(knob, "chamfer", 2, ([, , z]) => Math.abs(z) < 1e-6);
knob = fuse(knob, sphere([0, 0, 6], 10));
knob = blend(knob, "fillet", 2, ([x, y, z]) => Math.abs(z - 10) < 0.01 && Math.hypot(x, y) < 1); // the circle where the cap meets the top
knob = cut(knob, cylinder([0, 0, -1], [0, 0, 1], 2.5, 30));

// A block with rounded vertical edges (tangent chains), a chamfered top, a counterbored hole and a pocket.
let block = box(-20, -15, 0, 40, 30, 20);
block = blend(block, "fillet", 4, ([x, y, z]) => Math.abs(z - 10) < 0.01 && Math.abs(Math.abs(x) - 20) < 0.01 && Math.abs(Math.abs(y) - 15) < 0.01);
block = blend(block, "chamfer", 1, ([, , z]) => Math.abs(z - 20) < 0.01);
block = cut(block, cylinder([-8, 0, -1], [0, 0, 1], 3, 30));
block = cut(block, cylinder([-8, 0, 16], [0, 0, 1], 5, 10));
block = cut(block, box(4, -8, 12, 10, 16, 10));

// Two bodies in one file: a plate and a peg standing apart (the importer gives one mesh each).
const plate = box(0, 0, 0, 30, 20, 4);
const peg = fuse(cylinder([50, 10, 0], [0, 0, 1], 4, 12), box(44, 4, 0, 12, 12, 3));

const dir = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "core", "tests", "corpus_step");
fs.mkdirSync(dir, { recursive: true });
for (const [name, shapes] of [["gen_knob", [knob]], ["gen_block", [block]], ["gen_two_bodies", [plate, peg]]]) {
  const writer = new oc.STEPControl_Writer();
  for (const s of shapes) writer.Transfer(unify(s), oc.STEPControl_StepModelType.STEPControl_AsIs, true, range());
  writer.Write("/out.step");
  // The header's time stamp would make every run a new file: fix it.
  const text = new TextDecoder().decode(oc.FS.readFile("/out.step")).replace(/FILE_NAME\('[^']*','[^']*'/, "FILE_NAME('Open CASCADE Shape Model','2026-01-01T00:00:00'");
  fs.writeFileSync(path.join(dir, `${name}.step`), text);
  console.log(`wrote ${name}.step`);
}
