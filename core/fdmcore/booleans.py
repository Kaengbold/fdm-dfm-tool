"""Mesh booleans for local mode (needs ``pip install manifold3d``).

The browser uses the manifold-3d WASM build instead (web/geometry.js).
"""

from __future__ import annotations

import numpy as np

from .mesh import Mesh


def _to_manifold(tris: np.ndarray):
    from manifold3d import Manifold
    from manifold3d import Mesh as MMesh

    v = tris.reshape(-1, 3).astype(np.float32)
    mesh = MMesh(vert_properties=v, tri_verts=np.arange(len(v), dtype=np.uint32).reshape(-1, 3))
    mesh.merge()
    return Manifold(mesh)


def inspect_mesh(part: Mesh) -> dict:
    """Closedness, separate piece count and volume of ``part`` according to manifold3d."""
    from manifold3d import Error

    m = _to_manifold(part.triangles)
    closed = m.status() == Error.NoError
    return {
        "closed": closed,
        "status": str(m.status()),
        "pieces": len(m.decompose()) if closed else 0,
        "volume": float(m.volume()) if closed else 0.0,
    }


GROW_TOL_MM = 0.05  # as features.GROW_TOL_MM: outer-size changes below this are noise
VOLUME_TOL_MM3 = 1e-3
VOLUME_SLACK = 1.05  # a change moves at most its own pieces' volume (plus rounding)


def apply_edits(part: Mesh, edits: list[dict], refused: list[dict] | None = None, check: bool = True) -> Mesh:
    """Apply design edits (from ``api.design``): additions first, then cuts, piece by piece.

    With ``check``, every edit's step is checked against the model before it (T-434): the result is a
    closed solid, the volume moved the right way and by no more than the edit's own pieces, the outer
    size didn't grow (unless the edit says by how much, ``grows_mm``) and the number of separate pieces
    didn't change. An edit that fails is left out, the others are kept, and ``refused`` gets
    ``{"id", "title", "reason"}`` for it in plain words. Edits ``placed_by_user`` (blocks, another model)
    may change the size and the pieces: the user chose that. ``check=False`` (the split tool, which cuts
    the part apart on purpose) only applies them."""
    left_out: dict[str, str] = {}
    while True:  # an edit that both adds and cuts can fail after its addition is in: start again without it
        result, failed = _apply(part, [e for e in edits if e.get("id") not in left_out], check)
        if failed is None:
            break
        left_out[failed[0]] = failed[1]
    if refused is not None:
        refused += [{"id": e["id"], "title": e.get("title", e["id"]), "reason": left_out[e["id"]]}
                    for e in edits if e.get("id") in left_out]
    out = result.to_mesh()
    return Mesh(out.vert_properties[out.tri_verts.reshape(-1)].reshape(-1, 3, 3).astype(np.float64))


def _apply(part: Mesh, edits: list[dict], check: bool):
    """(result, None), or (None, (edit id, reason)) for the first edit that fails a check."""
    from manifold3d import Error

    original = result = _to_manifold(part.triangles)
    if original.status() != Error.NoError:
        raise ValueError(f"The part is not a closed (watertight) mesh: {original.status()}")
    pieces_before = _solids(original)
    for op in ("add", "cut"):
        for e in edits:
            ops = e.get("piece_ops") or [e.get("op", "cut")] * len(e["pieces"])  # grip fins: both in one edit
            tris = np.frombuffer(e["mesh"], "<f4").reshape(-1, 3, 3)
            ends = np.cumsum(e["pieces"])
            shapes = [_to_manifold(tris[end - n:end]) for n, end, piece_op in zip(e["pieces"], ends, ops) if piece_op == op]
            if not shapes:
                continue
            if any(s.status() != Error.NoError for s in shapes):
                if not check:
                    raise ValueError(f"Design edit {e['id']} is not a closed (watertight) mesh")
                return None, (e["id"], "its shape isn't closed, so it can't be joined to the part")
            step = result
            for s in shapes:
                step = step + s if op == "add" else step - s
                # Evaluate now: left to evaluate as one batch, manifold3d got 16 overlapping chamfer
                # pieces wrong (the union lost 224 mm3 instead of adding 148, T-434).
                step.status()
            step = _drop_loose(step, original)
            if check:
                reason = _step_problem(result, step, shapes, op, e, pieces_before)
                if reason:
                    return None, (e["id"], reason)
            result = step
    return result, None


def _step_problem(before, after, shapes: list, op: str, edit: dict, pieces_before: int) -> str | None:
    """Why applying ``edit`` (its ``op`` pieces, ``shapes``) to ``before`` gives a bad ``after``, or None."""
    from manifold3d import Error

    if after.status() != Error.NoError or after.is_empty():
        return "the result wouldn't be a closed solid"
    change = after.volume() - before.volume()
    if op == "add" and change < -VOLUME_TOL_MM3:
        return "it would take plastic away instead of adding it"
    if op == "cut" and change > VOLUME_TOL_MM3:
        return "it would add plastic instead of taking it away"
    if abs(change) > VOLUME_SLACK * sum(s.volume() for s in shapes) + VOLUME_TOL_MM3:
        return "it would change much more of the part than the change itself"
    if edit.get("placed_by_user"):
        return None
    lo0, hi0 = np.reshape(before.bounding_box(), (2, 3))
    lo1, hi1 = np.reshape(after.bounding_box(), (2, 3))
    grow = np.maximum(lo0 - lo1, 0.0) + np.maximum(hi1 - hi0, 0.0)
    allowed = np.asarray(edit.get("grows_mm") or [0.0, 0.0, 0.0], float) + GROW_TOL_MM
    if np.any(grow > allowed):
        axis = int(np.argmax(grow - allowed))
        return f"it would make the part {grow[axis]:.1f} mm bigger in {'XYZ'[axis]}"
    pieces = _solids(after)
    if pieces > pieces_before:
        return "it would cut the part in two" if pieces == pieces_before + 1 else f"it would cut the part into {pieces} pieces"
    if pieces < pieces_before:
        return "it would join separate pieces of the part together"
    return None


def _solids(m) -> int:
    """Separate pieces of ``m``. ``decompose`` also returns each closed hollow inside (a magnet pocket)
    as a shell of its own, with negative volume: that's part of a piece, not one."""
    return sum(1 for p in m.decompose() if p.volume() > 0)


def _drop_loose(result, original):
    """Without the pieces that don't overlap the part: a bit of an addition that ends up apart from it
    (two changes meeting at a corner, a taper cut short) would print as a loose crumb. Pieces a cut
    splits off the part still overlap it and stay, and so do closed hollows inside (negative volume)."""
    from manifold3d import Manifold

    pieces = result.decompose()
    if len(pieces) == 1:
        return result
    keep = [m for m in pieces if m.volume() < 0 or (m ^ original).volume() > 1e-3]
    return result if len(keep) == len(pieces) else Manifold.compose(keep)
