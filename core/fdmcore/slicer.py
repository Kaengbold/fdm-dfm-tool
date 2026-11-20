"""Pseudo-slicer: cut an oriented mesh into 2D layer outlines.

This is not a toolpath slicer. It produces the solid cross-section of selected
layers (shapely geometry), which is enough to reason about overhangs, bridges,
thin walls and bed contact. The mesh must already be in print orientation with
its lowest point at z = 0.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import shapely
from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry

from .mesh import Mesh

# Slice planes are nudged off round numbers so they don't hit vertices exactly.
_Z_JITTER = 1.37e-4
# Outlines are simplified to this (mm). Far below a printed line, but fine CAD exports carry
# thousands of points per circle, and every later shapely operation scales with point count.
# Also drops notches only a few hundredths of a mm deep (3DBenchy has rows of 0.02 mm ones): Pyodide's
# GEOS 3.12 throws "depth mismatch" when it buffers such an outline, which kills the engine (T-351).
# Slicers close gaps that small anyway (OrcaSlicer / PrusaSlicer slice_closing_radius 0.049 mm).
# Hole sizes come from layer areas: this makes them at most ~0.03 mm smaller.
SIMPLIFY_MM = 0.025


@dataclass
class Layer:
    index: int  # index within the analysed layers
    z: float  # slice height (mm)
    dz: float  # distance to the previous analysed layer (first layer: its own height)
    geom: BaseGeometry  # solid cross-section (Polygon / MultiPolygon / empty)


def slice_mesh(mesh: Mesh, layer_height: float, max_layers: int = 160) -> list[Layer]:
    """Section the mesh at up to ``max_layers`` evenly spaced heights.

    The first slice is always the middle of the first printed layer. When the part
    has more layers than ``max_layers``, the rest are sampled evenly; ``dz`` then
    spans several real layers.
    """
    tris = mesh.triangles
    height = float(tris[..., 2].max())
    first = layer_height / 2
    n = max(1, min(int(np.ceil(height / layer_height)), max_layers))
    zs = np.linspace(first, max(first, height - layer_height / 2), n) + _Z_JITTER
    zmin, zmax = tris[..., 2].min(axis=1), tris[..., 2].max(axis=1)
    layers = []
    for i, z in enumerate(zs):
        dz = layer_height if i == 0 else float(z - zs[i - 1])
        geom = section(tris[(zmin < z) & (zmax > z)], float(z))
        layers.append(Layer(i, float(z), dz, geom))
    return layers


def section(tris: np.ndarray, z: float) -> BaseGeometry:
    """Solid cross-section of a closed triangle soup at height z (even-odd fill)."""
    if len(tris) == 0:
        return Polygon()
    a = tris
    b = tris[:, [1, 2, 0]]
    da, db = a[..., 2] - z, b[..., 2] - z
    crossing = da * db < 0  # (N, 3) edges that cross the plane
    # Order each edge low->high so the shared edge of two neighbouring triangles
    # yields bit-identical points; that keeps the segment loops connected.
    swap = (da > db)[..., None]
    lo, hi = np.where(swap, b, a), np.where(swap, a, b)
    t = (z - lo[..., 2]) / (hi[..., 2] - lo[..., 2] + (~crossing))
    pts = lo[..., :2] + t[..., None] * (hi[..., :2] - lo[..., :2])
    ok = crossing.sum(axis=1) == 2
    segs = pts[ok][crossing[ok]].reshape(-1, 2, 2)
    t = tris[ok]
    normals = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])[:, :2]
    return _segments_to_solid(segs, normals)


def _segments_to_solid(segs: np.ndarray, normals: np.ndarray | None = None) -> BaseGeometry:
    polys = _chain_rings(segs, normals) if normals is not None else None
    if polys is None:
        polys = _merge_rings(segs)  # open or inconsistently wound mesh: slower, more forgiving
    if len(polys) == 0:
        return Polygon()
    polys = shapely.simplify(polys, SIMPLIFY_MM, preserve_topology=False)  # invalid results are repaired below
    bad = ~shapely.is_valid(polys)
    if bad.any():
        polys[bad] = shapely.buffer(polys[bad], 0)
    rings = [g for g in polys if g.area > 1e-6]
    if not rings:
        return Polygon()
    solid = _even_odd(rings)
    # Only repair what is broken. In Pyodide a GEOS error aborts the whole engine (try/except
    # can't catch it), and grid-snapping every outline (set_precision) made later buffers throw.
    if not solid.is_empty and not solid.is_valid:
        solid = shapely.union_all(parts(shapely.make_valid(solid)))
    return solid


def _chain_rings(segs: np.ndarray, normals: np.ndarray) -> np.ndarray | None:
    """Closed loops from cut segments, fully vectorised; None if the loops don't close cleanly.

    Each segment is turned so the solid lies on its left (from the triangle's outward normal);
    then every point starts exactly one segment and "next segment" is a permutation whose
    cycles are the loops. Loop membership and order come from pointer jumping (log n passes)."""
    n = len(segs)
    if n < 3:
        return None
    d = segs[:, 1] - segs[:, 0]
    flip = d[:, 0] * -normals[:, 1] + d[:, 1] * normals[:, 0] < 0
    segs = np.where(flip[:, None, None], segs[:, ::-1], segs)
    pts = np.ascontiguousarray(segs.reshape(-1, 2))
    _, ids = np.unique(pts.view(np.dtype((np.void, 16))).ravel(), return_inverse=True)
    ids = ids.reshape(-1, 2)
    start, end = ids[:, 0], ids[:, 1]
    first = np.full(ids.max() + 1, -1)
    first[start] = np.arange(n)
    if np.bincount(start).max() > 1:
        return None
    nxt = first[end]
    if (nxt < 0).any():
        return None
    rounds = int(np.ceil(np.log2(n))) + 1
    label, jump = np.arange(n), nxt.copy()
    for _ in range(rounds):  # label = smallest segment index in the loop
        label = np.minimum(label, label[jump])
        jump = jump[jump]
    # Cut each loop before its leader and count steps to that end: larger = earlier in the loop.
    last = nxt == label[nxt]
    step = np.where(last, np.arange(n), nxt)
    dist = (~last).astype(np.int64)
    for _ in range(rounds):
        dist = dist + dist[step]
        step = step[step]
    order = np.lexsort((-dist, label))
    lab = label[order]
    keep = np.bincount(lab)[lab] >= 3
    order, lab = order[keep], lab[keep]
    if len(order) == 0:
        return np.array([], dtype=object)
    _, ring_index = np.unique(lab, return_inverse=True)
    rings = shapely.linearrings(segs[order, 0], indices=ring_index.ravel())
    return np.asarray(shapely.polygons(rings))


def _merge_rings(segs: np.ndarray) -> np.ndarray:
    merged = shapely.line_merge(shapely.multilinestrings(shapely.linestrings(segs)))
    out = []
    for line in getattr(merged, "geoms", [merged]):
        coords = np.asarray(line.coords)
        if len(coords) < 4:
            continue
        if not line.is_ring:
            # Small gaps come from dirty meshes; close them, drop anything badly open.
            if np.linalg.norm(coords[0] - coords[-1]) > 0.05:
                continue
            coords = np.vstack([coords, coords[:1]])
        out.append(Polygon(coords))
    return np.array(out, dtype=object)


def _even_odd(polys: list[BaseGeometry]) -> BaseGeometry:
    """Combine loop interiors by nesting depth: even depth = solid, odd depth = hole."""
    tree = shapely.STRtree(polys)
    probes = shapely.points([_first_point(p) for p in polys])
    pairs = tree.query(probes, predicate="within")  # [probe idx, containing poly idx]
    depth = np.bincount(pairs[0], minlength=len(polys))
    parent = {}
    for child, container in zip(*pairs):
        # The direct parent is the container one level shallower.
        if depth[container] == depth[child] - 1:
            parent[child] = container
    pieces = []
    for i, poly in enumerate(polys):
        if depth[i] % 2:
            continue
        holes = [polys[c] for c, p in parent.items() if p == i]
        pieces.append(poly.difference(shapely.union_all(holes)) if holes else poly)
    return shapely.union_all(pieces)


def _first_point(poly: BaseGeometry):
    geom = poly.geoms[0] if hasattr(poly, "geoms") else poly
    return geom.exterior.coords[0]


def parts(geom: BaseGeometry, min_area: float = 0.0) -> list[Polygon]:
    """Polygons of a (multi)polygon result, dropping slivers below ``min_area``."""
    if geom.is_empty:
        return []
    polys = [g for g in getattr(geom, "geoms", [geom]) if isinstance(g, Polygon)]
    if not polys:
        return []
    # One vectorised call: the morphological checks split a curved layer into thousands of slivers (T-458).
    return [p for p, a in zip(polys, shapely.area(polys)) if a > min_area]
