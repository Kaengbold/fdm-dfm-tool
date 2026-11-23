// Applies design edits to a STEP part with OpenCascade the way the write-back worker does (web/brep.js applyEdits,
// T-529), once with their exact intents and once forced onto the faceted mesh path, and reports per edit how it was
// made, the face count, the volume change and whether the solid is valid. core/tests/test_brep_edits.py makes the
// edits with the engine and runs this. Dev-only (same packages as check_step_faces.mjs):
//   node tools/check_brep_edits.mjs PART.step EDITS.json
// EDITS.json: [{id, title, op, mesh: [floats], pieces, piece_ops?, intent?}].
import Module from "replicad-opencascadejs";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { performance } from "node:perf_hooks";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const copy = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "brep-")), "brep.mjs");
fs.copyFileSync(path.join(here, "..", "web", "brep.js"), copy); // web/ has no package.json: load it as a module
const B = await import(pathToFileURL(copy).href);

const [stepFile, editsFile] = process.argv.slice(2);
const edits = JSON.parse(fs.readFileSync(editsFile, "utf8")).map((e) => ({ ...e, mesh: Float32Array.from(e.mesh) }));
const oc = await Module();
oc.FS.writeFile("/in.step", new Uint8Array(fs.readFileSync(stepFile)));
const reader = new oc.STEPControl_Reader();
reader.ReadFile("/in.step");
reader.TransferRoots(new oc.Message_ProgressRange());
const solid = B.solids(oc, reader.OneShape())[0];
const before = { faces: B.faces(oc, solid).length, volume: B.volume(oc, solid), surfaces: B.surfaces(oc, solid) };

function run(list, exact) {
  const t = performance.now();
  const { shape, notes } = B.applyEdits(oc, solid, list, { exact });
  const mesh = B.tessellate(oc, shape);
  return { notes, ms: Math.round(performance.now() - t), valid: B.isValid(oc, shape), faces: B.faces(oc, shape).length, surfaces: B.surfaces(oc, shape),
    dV: B.volume(oc, shape) - before.volume, triangles: mesh.faceIds.length,
    meshFaces: new Set(mesh.faceIds).size };
}

const out = { before, edits: {} };
for (const e of edits) out.edits[e.id] = { exact: run([e], true), mesh: run([e], false) };
out.all = run(edits, true);
// The written file reads back as one valid solid with the same faces.
const { shape } = B.applyEdits(oc, solid, edits);
const writer = new oc.STEPControl_Writer();
writer.Transfer(shape, oc.STEPControl_StepModelType.STEPControl_AsIs, true, new oc.Message_ProgressRange());
writer.Write("/out.step");
const back = new oc.STEPControl_Reader();
back.ReadFile("/out.step");
back.TransferRoots(new oc.Message_ProgressRange());
const reread = B.solids(oc, back.OneShape());
out.written = { solids: reread.length, faces: reread[0] ? B.faces(oc, reread[0]).length : 0, valid: reread[0] ? B.isValid(oc, reread[0]) : false };
console.log(JSON.stringify(out));
