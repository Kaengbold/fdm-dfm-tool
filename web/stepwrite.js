// STEP write-back: the original CAD solid with design edits applied, by OpenCascade in a worker
// (stepwrite-worker.js), so the page's Content Security Policy needs no 'unsafe-eval' (T-327).
import { jobWorker } from "./jobworker.js";

const run = jobWorker(new URL("./stepwrite-worker.js", import.meta.url));

/**
 * The original STEP with design edits applied, as STEP bytes (Uint8Array).
 * ``bodyVolume`` picks the solid the edits belong to when the file has several (mm³).
 * ``edits``: [{op: "add"|"cut", mesh: Float32Array, pieces: [triangles per piece]}], in part (= STEP) coordinates.
 * The worker gets copies, so the caller's arrays stay usable.
 */
export async function stepWithEdits(stepBytes, bodyVolume, edits) {
  const plain = edits.map(({ op, title, mesh, pieces }) => ({ op, title, mesh, pieces }));
  return (await run({ stepBytes, bodyVolume, edits: plain })).out;
}
