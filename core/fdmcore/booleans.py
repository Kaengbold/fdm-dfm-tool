"""Mesh booleans for local mode (needs ``pip install manifold3d``).

The browser uses the manifold-3d WASM build instead (web/geometry.js).
"""

from __future__ import annotations

import numpy as np

from .mesh import Mesh


def subtract(part: Mesh, cutters: list[np.ndarray]) -> Mesh:
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
    for c in cutters:
        result = result - to_manifold(c, "A design edit")
    out = result.to_mesh()
    return Mesh(out.vert_properties[out.tri_verts.reshape(-1)].reshape(-1, 3, 3).astype(np.float64))
