// STEP import via occt-import-js (OpenCascade compiled to WASM, LGPL-2.1), loaded on first use.
const OCCT_BASE = "https://cdn.jsdelivr.net/npm/occt-import-js@0.0.23/dist/";
let occt = null;

// The browser refuses the file if it isn't the pinned version (T-312; same file as tools/vendor.py pins).
const OCCT_INTEGRITY = "sha384-Z8pO9JEhFZF+mvDKxMuLvPT85VP7nly8CqYjBatl0bMeW9OsaKCes4gEUPL4nihc";

function loadScript(src, integrity) {
  return new Promise((resolve, reject) => {
    const s = Object.assign(document.createElement("script"), { src, integrity, crossOrigin: "anonymous", onload: resolve, onerror: () => reject(new Error(`Could not load ${src}`)) });
    document.head.append(s);
  });
}

async function occtModule() {
  occt ??= loadScript(OCCT_BASE + "occt-import-js.js", OCCT_INTEGRITY).then(() =>
    // The script defines a global factory.
    globalThis.occtimportjs({ locateFile: (file) => OCCT_BASE + file }),
  );
  return occt;
}

/**
 * Read a STEP file into bodies, largest first. Each body has a triangle soup
 * (Float32Array, 9 floats per triangle, mm) and the CAD face id of every triangle.
 */
export async function readStep(bytes) {
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
