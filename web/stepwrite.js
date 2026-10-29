// STEP write-back: apply design edits to the original CAD solid with OpenCascade (replicad's
// WASM build, LGPL-2.1, ~23 MB, loaded on first use), so CAD users get their exact faces back
// and only the edited spots change. Edits arrive as closed triangle pieces (see geometry.js);
// each is sewn into a small solid, fused or cut, and coplanar triangles merged into plain faces.
const OC_URL = "https://cdn.jsdelivr.net/npm/replicad-opencascadejs@1.1.0/dist/replicad_single.js";
let ocPromise = null;

async function opencascade() {
  ocPromise ??= import(OC_URL).then(({ default: Module }) => Module({ locateFile: (f) => new URL(f, OC_URL).href }));
  return ocPromise;
}

const progress = (oc) => new oc.Message_ProgressRange();

function volume(oc, shape) {
  const props = new oc.GProp_GProps();
  oc.BRepGProp.VolumeProperties(shape, props, false, false, false);
  const v = props.Mass();
  props.delete();
  return v;
}

function solids(oc, shape) {
  const out = [];
  for (const ex = new oc.TopExp_Explorer(shape, oc.TopAbs_ShapeEnum.TopAbs_SOLID, oc.TopAbs_ShapeEnum.TopAbs_SHAPE); ex.More(); ex.Next()) {
    out.push(oc.TopoDS.Solid(ex.Current()));
  }
  return out;
}

/** A closed triangle soup (Float32Array, 9 floats per triangle) as an OpenCascade solid. */
function soupToSolid(oc, soup) {
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

/**
 * The original STEP with design edits applied, as STEP bytes.
 * ``bodyVolume`` picks the solid the edits belong to when the file has several (mm³).
 * ``edits``: [{op: "add"|"cut", mesh: Float32Array, pieces: [triangles per piece]}], in part (= STEP) coordinates.
 */
export async function stepWithEdits(stepBytes, bodyVolume, edits) {
  const oc = await opencascade();
  oc.FS.writeFile("/in.step", stepBytes);
  const reader = new oc.STEPControl_Reader();
  if (reader.ReadFile("/in.step") !== oc.IFSelect_ReturnStatus.IFSelect_RetDone) throw new Error("Could not read the original STEP file");
  reader.TransferRoots(progress(oc));
  const all = solids(oc, reader.OneShape());
  if (!all.length) throw new Error("No solid in the original STEP file");
  let shape = all.reduce((best, s) => (Math.abs(volume(oc, s) - bodyVolume) < Math.abs(volume(oc, best) - bodyVolume) ? s : best));
  for (const op of ["add", "cut"]) {
    for (const e of edits.filter((x) => x.op === op)) {
      let start = 0;
      for (const n of e.pieces) {
        const piece = soupToSolid(oc, e.mesh.subarray(start * 9, (start + n) * 9));
        start += n;
        const bool = op === "add" ? new oc.BRepAlgoAPI_Fuse(shape, piece, progress(oc)) : new oc.BRepAlgoAPI_Cut(shape, piece, progress(oc));
        if (!bool.IsDone()) throw new Error(`OpenCascade could not apply "${e.title}"`);
        shape = bool.Shape();
      }
    }
  }
  const unify = new oc.ShapeUpgrade_UnifySameDomain(shape, true, true, false);
  unify.Build();
  const writer = new oc.STEPControl_Writer();
  writer.Transfer(unify.Shape(), oc.STEPControl_StepModelType.STEPControl_AsIs, true, progress(oc));
  if (writer.Write("/out.step") !== oc.IFSelect_ReturnStatus.IFSelect_RetDone) throw new Error("Could not write the STEP file");
  const out = oc.FS.readFile("/out.step");
  for (const f of ["/in.step", "/out.step"]) oc.FS.unlink(f);
  return out;
}
