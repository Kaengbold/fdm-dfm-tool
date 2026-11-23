// STEP write-back worker (started by stepwrite.js): apply design edits to the original CAD solid
// with OpenCascade (replicad's WASM build, LGPL-2.1, ~23 MB, loaded on first use), so CAD users get
// their exact faces back. An edit with an exact description (its ``intent``, T-528) is made with exact
// tools: a resized hole stays one cylinder, a round is a real fillet. Otherwise, or when that fails, its
// closed triangle pieces (see geometry.js) are sewn into a small solid and fused or cut (web/brep.js,
// T-529). In a worker because OpenCascade's bindings build functions from strings, which the page's
// Content Security Policy forbids (T-327); a worker started from a file of this site doesn't take on
// the page's policy.
import { answerJobs, checkedFetch } from "./jobworker.js";
import { applyEdits, solids, tessellate, volume } from "./brep.js";

// Resolved against this module, so the self-hosted build can swap in a relative "../vendor/..." path.
const OC_URL = new URL("https://cdn.jsdelivr.net/npm/replicad-opencascadejs@1.1.0/dist/replicad_single.js", import.meta.url).href;
const WASM_URL = OC_URL.replace(/\.js$/, ".wasm");
// The page's import map can't check files a worker loads, so check them here: the browser refuses a
// file whose hash differs (same pinned files as tools/vendor.py).
const OC_INTEGRITY = "sha384-bn4+xCBX4YVTp9aJ4lTICP2hWAh2XeB7aSyDKbkS0JblHhimh0rzrg1JCm+j7nID";
const WASM_INTEGRITY = "sha256-TJ8i6fOCjcpvPJVAWTTNvmJOWTw1Jm9H85KrM3R4294=";
let ocPromise = null;

async function opencascade() {
  ocPromise ??= (async () => {
    const [js, wasm] = await Promise.all([checkedFetch(OC_URL, OC_INTEGRITY), checkedFetch(WASM_URL, WASM_INTEGRITY)]);
    // Run the checked text (importing the URL again would fetch it unchecked). It finds its files
    // relative to its own address, so give it the real one instead of the blob's.
    const text = (await js.text()).replaceAll("import.meta.url", JSON.stringify(OC_URL));
    const code = URL.createObjectURL(new Blob([text], { type: "text/javascript" }));
    try {
      const { default: Module } = await import(code);
      return await Module({ wasmBinary: await wasm.arrayBuffer() });
    } finally {
      URL.revokeObjectURL(code);
    }
  })();
  return ocPromise;
}

const progress = (oc) => new oc.Message_ProgressRange();

/**
 * The original STEP with design edits applied: {out: STEP bytes, notes (per edit: exact, mesh or skipped, and why),
 * soup + faceIds: the result's triangles and B-rep face of each, for showing it and checking it again}.
 * ``bodyVolume`` picks the solid the edits belong to when the file has several (mm³).
 * ``edits``: [{id, title, op, mesh: Float32Array, pieces, piece_ops?, intent?}], in part (= STEP) coordinates.
 */
async function stepWithEdits(stepBytes, bodyVolume, edits) {
  const oc = await opencascade();
  oc.FS.writeFile("/in.step", stepBytes);
  const reader = new oc.STEPControl_Reader();
  if (reader.ReadFile("/in.step") !== oc.IFSelect_ReturnStatus.IFSelect_RetDone) throw new Error("Could not read the original STEP file");
  reader.TransferRoots(progress(oc));
  const all = solids(oc, reader.OneShape());
  if (!all.length) throw new Error("No solid in the original STEP file");
  const solid = all.reduce((best, s) => (Math.abs(volume(oc, s) - bodyVolume) < Math.abs(volume(oc, best) - bodyVolume) ? s : best));
  const { shape, notes } = applyEdits(oc, solid, edits);
  const writer = new oc.STEPControl_Writer();
  writer.Transfer(shape, oc.STEPControl_StepModelType.STEPControl_AsIs, true, progress(oc));
  if (writer.Write("/out.step") !== oc.IFSelect_ReturnStatus.IFSelect_RetDone) throw new Error("Could not write the STEP file");
  const out = oc.FS.readFile("/out.step");
  for (const f of ["/in.step", "/out.step"]) oc.FS.unlink(f);
  return { out, notes, ...tessellate(oc, shape) };
}

answerJobs(async ({ stepBytes, bodyVolume, edits }) => {
  const result = await stepWithEdits(stepBytes, bodyVolume, edits);
  return { result, transfer: [result.out.buffer, result.soup.buffer, result.faceIds.buffer] };
});
