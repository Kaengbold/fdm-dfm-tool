"""Second look at the best orientations, with real layer outlines.

The first ranking (orientation.py) works on face normals only: it assumes every overhang
needs a support column down to the bed and counts any face touching z=0 as bed contact.
Here the part is sliced, so we know

- the real first-layer area,
- where support columns end: on the bed, or on the part (each landing leaves a rough scar),
- how much support is printed (volume),
- the risky bits of the first layer: small separate spots and thin strips on the bed.

Bridges (anchored on two sides, short enough for the material) need no support, as in a slicer.
"""

from __future__ import annotations

import numpy as np
import shapely
from shapely.geometry import Polygon

from .checks import _bridge_span, _opening, _safe_diff
from .mesh import Mesh
from .profile import Profile, load_data
from .slicer import slice_mesh

QUAD = 2


def support_stats(mesh: Mesh, profile: Profile, max_layers: int = 40) -> dict:
    """``mesh`` in print orientation (lowest point at z = 0)."""
    layers = slice_mesh(mesh, profile.layer_height, max_layers)
    if not layers:
        return {"bed_area": 0.0, "support_volume": 0.0, "support_area": 0.0, "scar_area": 0.0, "small_spots": 0, "thin_share": 0.0}
    tan_lim = np.tan(np.radians(profile.material["max_overhang_deg"]))
    lw = profile.line_width
    max_bridge = profile.material["max_bridge_mm"]
    # Unsupported part of each layer: what reaches further out than the overhang limit allows.
    need = [Polygon()]
    for below, layer in zip(layers, layers[1:]):
        reach = layer.dz * tan_lim + 0.5 * lw
        if layer.geom.is_empty:
            need.append(Polygon())
        elif below.geom.is_empty:
            need.append(layer.geom)
        else:
            free = _clean(layer.geom.difference(below.geom.buffer(reach, quad_segs=QUAD)))
            anchor_zone = below.geom.buffer(reach + lw, quad_segs=QUAD)
            keep = [piece for piece in getattr(free, "geoms", [free]) if not piece.is_empty
                    and not _bridgeable(piece, anchor_zone, max_bridge)]
            need.append(shapely.union_all(keep) if keep else Polygon())
    support_area = sum(g.area for g in need)
    # Carry support columns down from the top; a column stops where it meets the part.
    column = Polygon()
    volume = scar = 0.0
    for i in range(len(layers) - 1, 0, -1):
        column = _clean(shapely.union(column, need[i])) if not need[i].is_empty else column
        if column.is_empty:
            continue
        solid = layers[i - 1].geom
        if not solid.is_empty:
            scar += column.intersection(solid).area
            column = _clean(column.difference(solid))
        volume += column.area * layers[i].dz
    return {
        "bed_area": float(layers[0].geom.area),
        "support_volume": float(volume),
        "support_area": float(support_area),
        "scar_area": float(scar),
        **first_layer_stats(layers[0].geom, profile),
    }


def first_layer_stats(geom, profile: Profile) -> dict:
    """What can come loose in the first layer: separate spots smaller than ``island_min_mm2`` (a
    tip, a thin post, a corner touching the bed) and the share of the rest in strips narrower than
    ``thin_lines`` line widths. A simple first layer is one solid patch."""
    T = load_data("thresholds")["first_layer"]
    pieces = [g for g in getattr(geom, "geoms", [geom]) if not g.is_empty]
    small = [g for g in pieces if g.area < T["island_min_mm2"]]
    big = [g for g in pieces if g.area >= T["island_min_mm2"]]
    area = sum(g.area for g in big)
    thin = 0.0
    if area > 0:
        rest = shapely.union_all(big)
        thin = _safe_diff(rest, _opening(rest, 0.5 * T["thin_lines"] * profile.line_width)).area / area
    return {"small_spots": len(small), "thin_share": float(thin)}


def _bridgeable(piece, anchor_zone, max_bridge: float) -> bool:
    span, anchors = _bridge_span(piece, anchor_zone)
    return anchors >= 2 and span <= max_bridge


def _clean(g):
    """Drop slivers so tiny leftovers don't grow into fake support columns."""
    if g.is_empty:
        return g
    polys = [p for p in getattr(g, "geoms", [g]) if isinstance(p, Polygon) and p.area > 0.5]
    return shapely.union_all(polys) if polys else Polygon()
