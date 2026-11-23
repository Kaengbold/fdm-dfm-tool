// STEP write-back: the original CAD solid with design edits applied, by OpenCascade in a worker
// (stepwrite-worker.js), so the page's Content Security Policy needs no 'unsafe-eval' (T-327).
import { jobWorker } from "./jobworker.js";

const run = jobWorker(new URL("./stepwrite-worker.js", import.meta.url));

/**
 * The original STEP with design edits applied (T-529): {out: STEP bytes (Uint8Array), notes: per edit {id, title,
 * how: "exact" | "mesh" | "skipped", why?}, soup + faceIds: the result's triangles and B-rep face of each}.
 * ``bodyVolume`` picks the solid the edits belong to when the file has several (mm³).
 * ``edits``: [{id, op: "add"|"cut", mesh: Float32Array, pieces: [triangles per piece], piece_ops?, intent?}], in part
 * (= STEP) coordinates; ``intent`` (the engine's exact description, T-528) is made with exact CAD tools.
 * The worker gets copies, so the caller's arrays stay usable.
 */
export async function stepWithEdits(stepBytes, bodyVolume, edits) {
  const plain = edits.map(({ id, op, title, mesh, pieces, piece_ops, intent }) => ({ id, op, title, mesh, pieces, piece_ops, intent }));
  return run({ stepBytes, bodyVolume, edits: plain });
}
