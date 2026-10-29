// Mesh booleans in the browser via manifold-3d (WASM, Apache-2.0), loaded on first use.
const MANIFOLD_URL = "https://cdn.jsdelivr.net/npm/manifold-3d@3.5.4/manifold.js";
let wasm = null;

async function manifold() {
  wasm ??= import(MANIFOLD_URL).then(async ({ default: Module }) => {
    const w = await Module();
    w.setup();
    return w;
  });
  return wasm;
}

/** Apply design edits to a part soup (Float32Array, 9 floats per triangle). Each edit is
 * {op: "add"|"cut", mesh: Float32Array, pieces: [triangle count per closed piece]};
 * additions go first, then cuts, one closed piece at a time. */
export async function applyEdits(part, edits) {
  const { Manifold, Mesh } = await manifold();
  const toManifold = (soup, what) => {
    const mesh = new Mesh({ numProp: 3, vertProperties: soup, triVerts: Uint32Array.from({ length: soup.length / 3 }, (_, i) => i) });
    mesh.merge();
    const m = new Manifold(mesh);
    if (m.status() !== "NoError") throw new Error(`${what} is not a closed (watertight) mesh: ${m.status()}`);
    return m;
  };
  let result = toManifold(part, "The part");
  for (const op of ["add", "cut"]) {
    for (const e of edits.filter((x) => x.op === op)) {
      let start = 0;
      for (const n of e.pieces) {
        const piece = toManifold(e.mesh.subarray(start * 9, (start + n) * 9), `The design change "${e.title}"`);
        result = op === "add" ? result.add(piece) : result.subtract(piece);
        start += n;
      }
    }
  }
  const out = result.getMesh();
  const soup = new Float32Array(out.triVerts.length * 3);
  out.triVerts.forEach((v, i) => soup.set(out.vertProperties.subarray(v * out.numProp, v * out.numProp + 3), i * 3));
  return soup;
}

/** Binary STL bytes from a triangle soup (normals left 0; slicers recompute them). */
export function stlBytes(soup) {
  const count = soup.length / 9;
  const view = new DataView(new ArrayBuffer(84 + count * 50));
  view.setUint32(80, count, true);
  for (let f = 0; f < count; f++) {
    const off = 84 + f * 50 + 12;
    for (let k = 0; k < 9; k++) view.setFloat32(off + k * 4, soup[f * 9 + k], true);
  }
  return new Uint8Array(view.buffer);
}
