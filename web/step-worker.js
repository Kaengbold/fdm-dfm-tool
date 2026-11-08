// STEP import worker (started by step.js): occt-import-js (OpenCascade compiled to WASM, LGPL-2.1),
// loaded on first use. In a worker because the library builds functions from strings, which the
// page's Content Security Policy forbids (T-327).
import { answerJobs, checkedFetch } from "./jobworker.js";

// Resolved against this module, so the self-hosted build can swap in a relative "../vendor/..." path.
const OCCT_BASE = new URL("https://cdn.jsdelivr.net/npm/occt-import-js@0.0.23/dist/", import.meta.url).href;
// The browser refuses a file that isn't the pinned version (T-312; same files as tools/vendor.py pins).
const OCCT_INTEGRITY = "sha384-Z8pO9JEhFZF+mvDKxMuLvPT85VP7nly8CqYjBatl0bMeW9OsaKCes4gEUPL4nihc";
const WASM_INTEGRITY = "sha256-MzkfydlOpchppnGEiL8KmkZCIrrJvcdk3+FpDO8oGVI=";
let occt = null;

async function occtModule() {
  occt ??= (async () => {
    const [js, wasm] = await Promise.all([
      checkedFetch(OCCT_BASE + "occt-import-js.js", OCCT_INTEGRITY),
      checkedFetch(OCCT_BASE + "occt-import-js.wasm", WASM_INTEGRITY),
    ]);
    // A plain script that defines a global factory: run the checked text in the worker's global scope.
    (0, eval)(await js.text());
    return globalThis.occtimportjs({ wasmBinary: await wasm.arrayBuffer() });
  })();
  return occt;
}

/** Bodies of a STEP file, largest first (see step.js). */
async function readStep(bytes) {
  const lib = await occtModule();
  const r = lib.ReadStepFile(bytes, {
    linearUnit: "millimeter",
    linearDeflectionType: "bounding_box_ratio",
    linearDeflection: 0.0005,
    angularDeflection: 0.3,
  });
  if (!r.success) throw new Error("Could not read this STEP file");
  const bodies = r.meshes.map((m, i) => {
    const pos = m.attributes.position.array;
    const idx = m.index.array;
    const soup = new Float32Array(idx.length * 3);
    for (let k = 0; k < idx.length; k++) soup.set([pos[idx[k] * 3], pos[idx[k] * 3 + 1], pos[idx[k] * 3 + 2]], k * 3);
    const faceIds = new Uint32Array(idx.length / 3);
    (m.brep_faces ?? []).forEach((f, id) => faceIds.fill(id, f.first, f.last + 1));
    return { name: m.name || `Body ${i + 1}`, soup, faceIds, volume: Math.abs(signedVolume(soup)) };
  });
  return bodies.filter((b) => b.soup.length).sort((a, b) => b.volume - a.volume);
}

function signedVolume(t) {
  let v = 0;
  for (let i = 0; i < t.length; i += 9) {
    const [ax, ay, az, bx, by, bz, cx, cy, cz] = t.subarray(i, i + 9);
    v += ax * (by * cz - bz * cy) - ay * (bx * cz - bz * cx) + az * (bx * cy - by * cx);
  }
  return v / 6;
}

answerJobs(async ({ bytes }) => {
  const bodies = await readStep(bytes);
  return { result: { bodies }, transfer: bodies.flatMap((b) => [b.soup.buffer, b.faceIds.buffer]) };
});
