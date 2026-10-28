"""Group triangles into user-meaningful faces ("regions").

Two passes over the triangle adjacency graph:
1. exact planar patches: neighbours with (almost) identical normals;
2. small patches (strips of a tessellated cylinder, fillet, sphere) are merged
   with smooth small neighbours, so a hole wall becomes one region while large
   flat faces stay separate from the fillets around them.
"""

from __future__ import annotations

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from .mesh import Mesh


def weld(mesh: Mesh, decimals: int = 5) -> np.ndarray:
    """Vertex index per triangle corner (M, 3)."""
    pts = np.round(mesh.triangles.reshape(-1, 3), decimals)
    _, inverse = np.unique(pts, axis=0, return_inverse=True)
    return inverse.reshape(-1, 3)


def face_adjacency(faces: np.ndarray) -> np.ndarray:
    """Pairs of triangles sharing an edge (K, 2)."""
    edges = np.sort(np.stack([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]], axis=1).reshape(-1, 2), axis=1)
    owner = np.repeat(np.arange(len(faces)), 3)
    order = np.lexsort((edges[:, 1], edges[:, 0]))
    e, o = edges[order], owner[order]
    same = np.all(e[1:] == e[:-1], axis=1)
    return np.stack([o[:-1][same], o[1:][same]], axis=1)


def find_regions(mesh: Mesh, planar_deg: float = 0.5, smooth_deg: float = 20.0, small_faces: int = 4) -> np.ndarray:
    """Region id per triangle."""
    normals, _ = mesh.normals_and_areas()
    adj = face_adjacency(weld(mesh))
    cos = np.einsum("ij,ij->i", normals[adj[:, 0]], normals[adj[:, 1]])
    n = mesh.face_count

    planar = adj[cos > np.cos(np.radians(planar_deg))]
    _, patch = _components(planar, n)
    size = np.bincount(patch)
    small = size[patch] <= small_faces
    smooth = (cos > np.cos(np.radians(smooth_deg))) & small[adj[:, 0]] & small[adj[:, 1]]
    _, region = _components(np.vstack([planar, adj[smooth]]), n)
    return region


def region_info(mesh: Mesh, region: np.ndarray) -> list[dict]:
    normals, areas = mesh.normals_and_areas()
    centroids = mesh.triangles.mean(axis=1)
    out = []
    for r in range(region.max() + 1):
        sel = region == r
        a = areas[sel]
        n = (normals[sel] * a[:, None]).sum(axis=0)
        flatness = float(np.linalg.norm(n) / max(a.sum(), 1e-12))  # 1 = planar
        out.append({
            "id": r,
            "area_mm2": float(a.sum()),
            "normal": (n / max(np.linalg.norm(n), 1e-12)).tolist(),
            "centroid": (centroids[sel] * a[:, None]).sum(axis=0).__truediv__(max(a.sum(), 1e-12)).tolist(),
            "kind": "flat" if flatness > 0.999 else "curved",
        })
    return out


def _components(pairs: np.ndarray, n: int):
    g = coo_matrix((np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(n, n))
    return connected_components(g, directed=False)
