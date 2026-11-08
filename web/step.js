// STEP import: occt-import-js (OpenCascade compiled to WASM, LGPL-2.1, ~8 MB, loaded on first use)
// in a worker (step-worker.js), so the page's Content Security Policy needs no 'unsafe-eval' (T-327).
import { jobWorker } from "./jobworker.js";

const run = jobWorker(new URL("./step-worker.js", import.meta.url));

/**
 * Read a STEP file into bodies, largest first. Each body has a triangle soup
 * (Float32Array, 9 floats per triangle, mm), the CAD face id of every triangle, a name and its volume.
 * The worker gets a copy of ``bytes``.
 */
export async function readStep(bytes) {
  return (await run({ bytes })).bodies;
}
