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
    return _segments_to_solid(segs)


def _segments_to_solid(segs: np.ndarray) -> BaseGeometry:
    merged = shapely.line_merge(shapely.multilinestrings(shapely.linestrings(segs)))
    lines = list(getattr(merged, "geoms", [merged]))
    rings = []
    for line in lines:
        coords = np.asarray(line.coords)
        if len(coords) < 4:
            continue
        if not line.is_ring:
            # Small gaps come from dirty meshes; close them, drop anything badly open.
            if np.linalg.norm(coords[0] - coords[-1]) > 0.05:
                continue
            coords = np.vstack([coords, coords[:1]])
        poly = Polygon(coords)
        if not poly.is_valid:
            poly = poly.buffer(0)
        if poly.area > 1e-6:
            rings.append(poly)
    if not rings:
        return Polygon()
    solid = _even_odd(rings)
    if solid.is_empty:
        return solid
    try:
        return shapely.set_precision(solid, 1e-4)
    except Exception:
        return shapely.make_valid(solid)


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
    return [p for p in polys if p.area > min_area]
