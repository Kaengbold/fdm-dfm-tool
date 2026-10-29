"""Mesh booleans for local mode (needs ``pip install manifold3d``).

The browser uses the manifold-3d WASM build instead (web/geometry.js).
"""

from __future__ import annotations

import numpy as np

from .mesh import Mesh


def apply_edits(part: Mesh, edits: list[dict]) -> Mesh:
    """Apply design edits (from ``api.design``): additions first, then cuts, piece by piece."""
    from manifold3d import Error, Manifold
    from manifold3d import Mesh as MMesh

    def to_manifold(tris: np.ndarray, what: str):
        v = tris.reshape(-1, 3).astype(np.float32)
        mesh = MMesh(vert_properties=v, tri_verts=np.arange(len(v), dtype=np.uint32).reshape(-1, 3))
        mesh.merge()
        m = Manifold(mesh)
        if m.status() != Error.NoError:
            raise ValueError(f"{what} is not a closed (watertight) mesh: {m.status()}")
        return m

    result = to_manifold(part.triangles, "The part")
    for op in ("add", "cut"):
        for e in edits:
            if e.get("op", "cut") != op:
                continue
            tris = np.frombuffer(e["mesh"], "<f4").reshape(-1, 3, 3)
            start = 0
            for n in e["pieces"]:
                piece = to_manifold(tris[start:start + n], f"Design edit {e['id']}")
                result = result + piece if op == "add" else result - piece
                start += n
    out = result.to_mesh()
    return Mesh(out.vert_properties[out.tri_verts.reshape(-1)].reshape(-1, 3, 3).astype(np.float64))
