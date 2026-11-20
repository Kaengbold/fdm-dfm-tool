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

// Checks after each change, as core/fdmcore/booleans.py (T-434).
const GROW_TOL_MM = 0.05;
const VOLUME_TOL_MM3 = 1e-3;
const VOLUME_SLACK = 1.05; // a change moves at most its own pieces' volume (plus rounding)

/** Apply design edits to a part soup (Float32Array, 9 floats per triangle). Each edit is
 * {op: "add"|"cut", mesh: Float32Array, pieces: [triangle count per closed piece], piece_ops?: op per piece};
 * additions go first, then cuts, one closed piece at a time; loose leftovers are dropped.
 * Every edit's step is checked against the model before it: a closed solid, the volume moved the right
 * way and by no more than the edit's own pieces, the outer size didn't grow (unless the edit says by how
 * much, grows_mm) and the number of separate pieces didn't change (blocks and models placed_by_user may
 * change size and pieces). An edit that fails is left out and the others kept. check: false (the split
 * preview, which cuts the part apart on purpose) only applies them.
 * Returns {soup, refused: [{id, title, reason}]}. */
export async function applyEdits(part, edits, { check = true } = {}) {
  const { Manifold, Mesh } = await manifold();
  const toManifold = (soup) => {
    const mesh = new Mesh({ numProp: 3, vertProperties: soup, triVerts: Uint32Array.from({ length: soup.length / 3 }, (_, i) => i) });
    mesh.merge();
    return new Manifold(mesh);
  };
  const original = toManifold(part);
  if (original.status() !== "NoError") throw new Error(`The part is not a closed (watertight) mesh: ${original.status()}`);
  const piecesBefore = solids(original);
  // Drop pieces that don't overlap the part: a bit of an addition that ends up apart from it (two
  // changes meeting at a corner, a taper cut short) would print as a loose crumb. Pieces a cut
  // splits off the part still overlap it and stay, and so do closed hollows inside (negative volume).
  const dropLoose = (m) => {
    const pieces = m.decompose();
    if (pieces.length === 1) return m;
    const keep = pieces.filter((p) => p.volume() < 0 || p.intersect(original).volume() > 1e-3);
    return keep.length < pieces.length ? Manifold.compose(keep) : m;
  };
  const apply = (list) => {
    let result = original;
    for (const op of ["add", "cut"]) {
      for (const e of list) {
        const shapes = [];
        let start = 0;
        e.pieces.forEach((n, i) => {
          if ((e.piece_ops?.[i] ?? e.op) === op) shapes.push(toManifold(e.mesh.subarray(start * 9, (start + n) * 9))); // grip fins add and cut in one edit
          start += n;
        });
        if (!shapes.length) continue;
        if (shapes.some((s) => s.status() !== "NoError")) {
          if (!check) throw new Error(`The design change "${e.title}" is not a closed (watertight) mesh`);
          return { failed: e, reason: "its shape isn't closed, so it can't be joined to the part" };
        }
        let step = result;
        for (const s of shapes) {
          step = op === "add" ? step.add(s) : step.subtract(s);
          // Evaluate now: left to evaluate as one batch, manifold got 16 overlapping chamfer pieces
          // wrong (the union lost 224 mm³ instead of adding 148, T-434).
          step.status();
        }
        step = dropLoose(step);
        const reason = check && stepProblem(result, step, shapes, op, e, piecesBefore);
        if (reason) return { failed: e, reason };
        result = step;
      }
    }
    return { result };
  };
  const refused = [];
  let todo = edits;
  let outcome;
  // An edit that both adds and cuts can fail after its addition is in: start again without it.
  while ((outcome = apply(todo)).failed) {
    refused.push({ id: outcome.failed.id, title: outcome.failed.title, reason: outcome.reason });
    todo = todo.filter((e) => e !== outcome.failed);
  }
  const out = outcome.result.getMesh();
  const soup = new Float32Array(out.triVerts.length * 3);
  out.triVerts.forEach((v, i) => soup.set(out.vertProperties.subarray(v * out.numProp, v * out.numProp + 3), i * 3));
  return { soup, refused };
}

/** Separate pieces of ``m``: decompose also returns each closed hollow inside (a magnet pocket) as a
 * shell of its own, with negative volume; that's part of a piece, not one. */
const solids = (m) => m.decompose().filter((p) => p.volume() > 0).length;

/** Why applying edit ``e`` (its ``op`` pieces, ``shapes``) to ``before`` gives a bad ``after``, or null. */
function stepProblem(before, after, shapes, op, e, piecesBefore) {
  if (after.status() !== "NoError" || after.isEmpty()) return "the result wouldn't be a closed solid";
  const change = after.volume() - before.volume();
  if (op === "add" && change < -VOLUME_TOL_MM3) return "it would take plastic away instead of adding it";
  if (op === "cut" && change > VOLUME_TOL_MM3) return "it would add plastic instead of taking it away";
  if (Math.abs(change) > VOLUME_SLACK * shapes.reduce((v, s) => v + s.volume(), 0) + VOLUME_TOL_MM3)
    return "it would change much more of the part than the change itself";
  if (e.placed_by_user) return null;
  const b0 = before.boundingBox(), b1 = after.boundingBox();
  const grow = [0, 1, 2].map((i) => Math.max(b0.min[i] - b1.min[i], 0) + Math.max(b1.max[i] - b0.max[i], 0));
  const over = grow.map((g, i) => g - ((e.grows_mm?.[i] ?? 0) + GROW_TOL_MM));
  const axis = over.indexOf(Math.max(...over));
  if (over[axis] > 0) return `it would make the part ${grow[axis].toFixed(1)} mm bigger in ${"XYZ"[axis]}`;
  const pieces = solids(after);
  if (pieces > piecesBefore) return pieces === piecesBefore + 1 ? "it would cut the part in two" : `it would cut the part into ${pieces} pieces`;
  if (pieces < piecesBefore) return "it would join separate pieces of the part together";
  return null;
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
