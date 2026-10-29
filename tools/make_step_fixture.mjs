// Writes tools/fixtures/l_bracket.step (an L-bracket with a sideways Ø8 and an upright Ø6 hole),
// the STEP part the browser test loads. Dev-only:
//   npm install --no-save --prefix tools replicad-opencascadejs@1.1.0
import Module from "replicad-opencascadejs";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const oc = await Module();
const range = () => new oc.Message_ProgressRange();
const box = (x, y, z, dx, dy, dz) => new oc.BRepPrimAPI_MakeBox(new oc.gp_Pnt(x, y, z), dx, dy, dz).Shape();
const cylinder = (p, dir, r, h) => new oc.BRepPrimAPI_MakeCylinder(new oc.gp_Ax2(new oc.gp_Pnt(...p), new oc.gp_Dir(...dir)), r, h).Shape();
let part = new oc.BRepAlgoAPI_Fuse(box(-30, -15, 0, 60, 30, 5), box(-30, -15, 0, 5, 30, 40), range()).Shape();
part = new oc.BRepAlgoAPI_Cut(part, cylinder([-40, 0, 28], [1, 0, 0], 4, 30), range()).Shape();
part = new oc.BRepAlgoAPI_Cut(part, cylinder([10, 0, -5], [0, 0, 1], 3, 20), range()).Shape();
const unify = new oc.ShapeUpgrade_UnifySameDomain(part, true, true, false);
unify.Build();
const writer = new oc.STEPControl_Writer();
writer.Transfer(unify.Shape(), oc.STEPControl_StepModelType.STEPControl_AsIs, true, range());
writer.Write("/out.step");
const out = path.join(path.dirname(fileURLToPath(import.meta.url)), "fixtures", "l_bracket.step");
fs.writeFileSync(out, oc.FS.readFile("/out.step"));
console.log(`wrote ${out}`);
