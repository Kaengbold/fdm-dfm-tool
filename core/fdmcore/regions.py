"""Group triangles into user-meaningful faces ("regions").

Two passes over the triangle adjacency graph:
1. exact planar patches: neighbours with (almost) identical normals;
2. small patches (strips of a tessellated cylinder, fillet, sphere) are merged
   with smooth small neighbours, so a hole wall becomes one region while large
   flat faces stay separate from the fillets around them.

A flat side of an extruded outline is often just two triangles, as "small" as a
fillet strip; without more care one click would select the whole wall around a
rounded outline. So strips only join neighbours of about their own width that
bend the same way (outward or inward): flat sides, round outside corners and
round inside corners each become their own face.
"""

from __future__ import annotations

import numpy as np

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


def find_regions(mesh: Mesh, planar_deg: float = 0.5, smooth_deg: float = 20.0, small_faces: int = 4,
                 width_ratio: float = 3.0, wide_mm: float = 1.0) -> np.ndarray:
    """Region id per triangle. Two smooth strips stay apart when one is more than ``width_ratio``
    times wider and at least ``wide_mm`` wide (a flat side next to fillet strips; the floor keeps a
    thin seam strip in a hole wall), or when one curves outward and the other inward."""
    normals, areas = mesh.normals_and_areas()
    adj = face_adjacency(weld(mesh))
    cos = np.einsum("ij,ij->i", normals[adj[:, 0]], normals[adj[:, 1]])
    n = mesh.face_count

    planar = adj[cos > np.cos(np.radians(planar_deg))]
    _, patch = _components(planar, n)
    size = np.bincount(patch)
    small = size[patch] <= small_faces
    smooth = (cos > np.cos(np.radians(smooth_deg))) & small[adj[:, 0]] & small[adj[:, 1]]

    width = _patch_width(mesh, patch, areas)
    pa, pb = patch[adj[:, 0]], patch[adj[:, 1]]
    wa, wb = width[pa], width[pb]
    same_width = (np.maximum(wa, wb) <= width_ratio * np.minimum(wa, wb)) | (np.maximum(wa, wb) < wide_mm)
    bend = _bend(mesh, normals, adj, smooth & (pa != pb))
    label = np.zeros(len(size))
    np.add.at(label, pa, bend)
    np.add.at(label, pb, bend)
    same_bend = np.sign(label[pa]) == np.sign(label[pb])
    _, region = _components(np.vstack([planar, adj[smooth & same_width & same_bend]]), n)
    return region


def _patch_width(mesh: Mesh, patch: np.ndarray, areas: np.ndarray) -> np.ndarray:
    """Rough width of each planar patch: its area over its longest triangle edge (a strip of two
    triangles: area / diagonal, about its short side)."""
    t = mesh.triangles
    longest = np.linalg.norm(t - np.roll(t, 1, axis=1), axis=2).max(axis=1)
    k = int(patch.max()) + 1
    edge = np.zeros(k)
    np.maximum.at(edge, patch, longest)
    return np.bincount(patch, areas, minlength=k) / np.maximum(edge, 1e-12)


def _bend(mesh: Mesh, normals: np.ndarray, adj: np.ndarray, use: np.ndarray) -> np.ndarray:
    """+1 where the surface bends outward across a triangle pair (a round outside corner), -1
    inward (a fillet in an inside corner, a hole wall), 0 for pairs not in ``use``. Going from one
    triangle to the next, the normal turns towards the step on an outward bend."""
    c = mesh.triangles.mean(axis=1)
    a, b = adj[:, 0], adj[:, 1]
    s = np.einsum("ij,ij->i", normals[b] - normals[a], c[b] - c[a])
    return np.where(use, np.sign(s), 0.0)


def region_info(mesh: Mesh, region: np.ndarray) -> list[dict]:
    """Area, mean normal, centroid and flat/curved per region (bincount sums: CAD exports have
    thousands of regions, and a mask per region would scan every triangle each time)."""
    normals, areas = mesh.normals_and_areas()
    centroids = mesh.triangles.mean(axis=1)
    k = int(region.max()) + 1
    area = np.bincount(region, areas, minlength=k)
    n = np.stack([np.bincount(region, normals[:, d] * areas, minlength=k) for d in range(3)], axis=1)
    c = np.stack([np.bincount(region, centroids[:, d] * areas, minlength=k) for d in range(3)], axis=1)
    length = np.linalg.norm(n, axis=1)
    flat = length / np.maximum(area, 1e-12) > 0.999  # 1 = planar
    normal = n / np.maximum(length, 1e-12)[:, None]
    centroid = c / np.maximum(area, 1e-12)[:, None]
    return [{"id": r, "area_mm2": float(area[r]), "normal": normal[r].tolist(), "centroid": centroid[r].tolist(),
             "kind": "flat" if flat[r] else "curved"} for r in range(k)]


def _components(pairs: np.ndarray, n: int):
    from scipy.sparse import coo_matrix  # here: weld / face_adjacency are used where scipy isn't loaded (checks)
    from scipy.sparse.csgraph import connected_components

    g = coo_matrix((np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(n, n))
    return connected_components(g, directed=False)
