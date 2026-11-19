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


def apply_edits(part: Mesh, edits: list[dict]) -> Mesh:
    """Apply design edits (from ``api.design``): additions first, then cuts, piece by piece."""
    from manifold3d import Error

    def to_manifold(tris: np.ndarray, what: str):
        m = _to_manifold(tris)
        if m.status() != Error.NoError:
            raise ValueError(f"{what} is not a closed (watertight) mesh: {m.status()}")
        return m

    original = result = to_manifold(part.triangles, "The part")
    for op in ("add", "cut"):
        for e in edits:
            ops = e.get("piece_ops") or [e.get("op", "cut")] * len(e["pieces"])  # grip fins: both in one edit
            tris = np.frombuffer(e["mesh"], "<f4").reshape(-1, 3, 3)
            start = 0
            for n, piece_op in zip(e["pieces"], ops):
                if piece_op == op:
                    piece = to_manifold(tris[start:start + n], f"Design edit {e['id']}")
                    result = result + piece if op == "add" else result - piece
                start += n
    result = _drop_loose(result, original)
    out = result.to_mesh()
    return Mesh(out.vert_properties[out.tri_verts.reshape(-1)].reshape(-1, 3, 3).astype(np.float64))


def _drop_loose(result, original):
    """Without the pieces that don't overlap the part: a bit of an addition that ends up apart from it
    (two changes meeting at a corner, a taper cut short) would print as a loose crumb. Pieces a cut
    splits off the part still overlap it and stay."""
    from manifold3d import Manifold

    pieces = result.decompose()
    keep = [m for m in pieces if (m ^ original).volume() > 1e-3]
    return result if len(keep) == len(pieces) else Manifold.compose(keep)
