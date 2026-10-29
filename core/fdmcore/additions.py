"""Design edits that add material: 45° chamfers under ledges, sacrificial bridge layers over
holes that start in mid-air, and thicker walls.

All functions take the part in print orientation (lowest point at z = 0) and return
closed triangle meshes in print coordinates; features.py moves them back into part
coordinates. Each edit is a list of closed *pieces* (prisms) that may overlap: the
app unions them one by one, so no piece has to be merged with another here.
"""

from __future__ import annotations

import numpy as np
import shapely
from shapely.geometry import LineString, MultiPoint, Point, Polygon

from .checks import QUAD, Tracker, _bridge_span, _opening, _safe_diff, _strip_width
from .mesh import Mesh
from .profile import Profile, load_data
from .slicer import parts, section, slice_mesh

CEILING_NZ = -0.985  # down-facing faces within ~10° of flat
OVERLAP = 0.2  # mm an added piece reaches into the part, so the union joins them
MAX_SACRIFICIAL_D = 25.0  # bigger openings aren't holes for screws or pins


# ---------------------------------------------------------------- geometry helpers


def extrude(poly: Polygon, z0: float, z1: float) -> np.ndarray | None:
    """Closed prism of a 2D polygon (holes allowed) between two heights, outward wound.
    None when the polygon can't be triangulated cleanly."""
    poly = shapely.geometry.polygon.orient(poly, 1.0)  # exterior CCW, holes CW
    caps = _triangulate(poly)
    if caps is None:
        return None
    bot = np.concatenate([caps[:, ::-1], np.full((len(caps), 3, 1), z0)], axis=2)
    top = np.concatenate([caps, np.full((len(caps), 3, 1), z1)], axis=2)
    walls = []
    for ring in [poly.exterior, *poly.interiors]:
        c = np.asarray(ring.coords)[:, :2]
        a, b = c[:-1], c[1:]
        a0, b0 = np.column_stack([a, np.full(len(a), z0)]), np.column_stack([b, np.full(len(b), z0)])
        a1, b1 = np.column_stack([a, np.full(len(a), z1)]), np.column_stack([b, np.full(len(b), z1)])
        walls += [np.stack([a0, b0, b1], axis=1), np.stack([a0, b1, a1], axis=1)]
    return np.concatenate([bot, top, *walls])


def _triangulate(poly: Polygon) -> np.ndarray | None:
    """CCW triangles (k, 3, 2) covering the polygon, using only its ring vertices.

    Delaunay of the vertices, keeping triangles inside the polygon. That only covers the
    polygon exactly when every outline edge is a Delaunay edge; the area check catches the
    rest (constrained triangulation needs shapely ≥ 2.1, which Pyodide doesn't ship)."""
    coords = shapely.get_coordinates(poly)
    if len(coords) < 4:
        return None
    tris = shapely.get_parts(shapely.delaunay_triangles(MultiPoint(coords)))
    if len(tris) == 0:
        return None
    shapely.prepare(poly)
    tris = tris[shapely.contains_properly(poly, shapely.centroid(tris))]
    if abs(shapely.area(tris).sum() - poly.area) > 1e-6 * poly.area + 1e-6:
        return None
    t = shapely.get_coordinates(tris).reshape(-1, 4, 2)[:, :3]
    cross = (t[:, 1, 0] - t[:, 0, 0]) * (t[:, 2, 1] - t[:, 0, 1]) - (t[:, 1, 1] - t[:, 0, 1]) * (t[:, 2, 0] - t[:, 0, 0])
    return np.where((cross < 0)[:, None, None], t[:, ::-1], t)


def sweep(profile: list[np.ndarray], start: np.ndarray, end: np.ndarray) -> np.ndarray:
    """Closed prism: convex 3D ``profile`` (given at ``start``) swept to ``end``. Outward wound."""
    bot = [q for q in profile]
    top = [q + (end - start) for q in profile]
    n = len(profile)
    tris = []
    for i in range(1, n - 1):
        tris.append([bot[0], bot[i + 1], bot[i]])
        tris.append([top[0], top[i], top[i + 1]])
    for i in range(n):
        j = (i + 1) % n
        tris += [[bot[i], bot[j], top[j]], [bot[i], top[j], top[i]]]
    t = np.array(tris)
    return t if Mesh(t).volume() > 0 else t[:, ::-1]


def ceilings(mesh: Mesh, min_z: float) -> list[tuple[float, Polygon]]:
    """Flat down-facing faces above ``min_z``, merged per height: (z, outline in XY)."""
    n, a = mesh.normals_and_areas()
    t = mesh.triangles
    z = t[..., 2].mean(axis=1)
    sel = (n[:, 2] < CEILING_NZ) & (a > 1e-6) & (z > min_z)
    if not sel.any():
        return []
    t, z = t[sel], z[sel]
    level = np.round(z / 0.05).astype(int)
    out = []
    for lv in np.unique(level):
        pick = level == lv
        flat = shapely.union_all(shapely.polygons(t[pick][..., :2]))
        flat = flat.simplify(0.01)
        if not flat.is_valid:
            flat = shapely.union_all(parts(shapely.make_valid(flat)))
        out += [(float(z[pick].mean()), p) for p in parts(flat, 1.0)]
    return out


def _below(mesh: Mesh, z: float):
    """Cross-section just under height z."""
    t = mesh.triangles
    zmin, zmax = t[..., 2].min(axis=1), t[..., 2].max(axis=1)
    return section(t[(zmin < z) & (zmax > z)], z)


# ---------------------------------------------------------------- T-040 chamfers under ledges


def ledge_chamfers(mesh: Mesh, profile: Profile, max_width: float) -> list[dict]:
    """A flat ledge sticking out from a wall needs support. Filling the corner under it at 45°
    (a gusset as wide as the ledge) makes the whole underside print without support."""
    lw, lh = profile.line_width, profile.layer_height
    max_bridge = profile.material["max_bridge_mm"]
    out = []
    for zc, ceiling in ceilings(mesh, 2 * lh):
        below = _below(mesh, zc - 0.05)
        if below.is_empty:
            continue
        anchor = below.buffer(0.1, quad_segs=QUAD)
        span, anchors = _bridge_span(ceiling, below.buffer(lw, quad_segs=QUAD))
        if anchors >= 2 and span <= max_bridge:
            continue  # a bridge the material spans fine
        if ceiling.exterior.intersection(anchor).length > 0.9 * ceiling.exterior.length:
            continue  # roof of a pocket or counterbore: walls all around, filling it changes the pocket
        wedges, widths = [], []
        for line in _lines(ceiling.boundary.intersection(anchor)):
            for prism, w in _line_wedges(line, ceiling, zc, max_width):
                wedges.append(prism)
                widths.append(w)
        if wedges:
            out.append({"z": zc, "pieces": wedges, "width": max(widths), "bbox": ceiling.bounds})
    return out


def _lines(geom) -> list[np.ndarray]:
    """Coordinates of the merged, simplified lines in a (multi)line geometry."""
    if geom.is_empty:
        return []
    lines = [g for g in getattr(geom, "geoms", [geom]) if g.geom_type in ("LineString", "LinearRing")]
    if not lines:
        return []
    merged = shapely.line_merge(shapely.multilinestrings(lines)) if len(lines) > 1 else lines[0]
    return [np.asarray(g.simplify(0.05).coords) for g in getattr(merged, "geoms", [merged]) if g.length > 0.5]


def _line_wedges(c: np.ndarray, ceiling: Polygon, zc: float, max_width: float):
    """Triangular prisms along each segment of the wall–ledge line ``c``."""
    closed = np.allclose(c[0], c[-1])
    segs = []
    for a, b in zip(c[:-1], c[1:]):
        L = float(np.linalg.norm(b - a))
        if L < 0.2:
            continue
        t = (b - a) / L
        n = np.array([-t[1], t[0]])
        mid = (a + b) / 2
        if not ceiling.contains(Point(mid + 0.3 * n)):
            n = -n
            if not ceiling.contains(Point(mid + 0.3 * n)):
                continue
        w = min(_reach(ceiling, a + (b - a) * f, n, max_width) for f in (0.1, 0.5, 0.9))
        d = min(w * 0.98, zc - 0.05)
        if w > max_width or d < 0.5:
            segs.append(None)
            continue
        segs.append((a, b, t, n, d))
    out = []
    for i, s in enumerate(segs):
        if s is None:
            continue
        a, b, t, n, d = s
        # At a corner that turns away from the ledge, neighbouring wedges leave a gap: lengthen
        # both into it (by d·tan(half the turn)) so they overlap like a mitred corner.
        ext = []
        for j, sign in ((i - 1, -1), (i + 1, 1)):
            if not closed and not 0 <= j < len(segs):
                ext.append(0.0)
                continue
            o = segs[j % len(segs)]
            if o is None or (o[3] @ t) * sign <= 0:
                ext.append(0.0)
                continue
            turn = np.arccos(np.clip(o[2] @ t, -1, 1))
            ext.append(d * np.tan(min(turn, np.radians(120)) / 2))
        a2, b2 = a - t * ext[0], b + t * ext[1]
        p3 = lambda p, z: np.array([p[0], p[1], z])
        prof = [p3(a2 - n * OVERLAP, zc + OVERLAP), p3(a2 + n * d, zc + OVERLAP), p3(a2 - n * OVERLAP, zc - d - OVERLAP)]
        out.append((sweep(prof, p3(a2, 0), p3(b2, 0)), d))
    return out


def _reach(poly: Polygon, p: np.ndarray, n: np.ndarray, limit: float) -> float:
    """How far the polygon extends from ``p`` in direction ``n`` (≥ limit + 1 when farther)."""
    ray = LineString([p, p + n * (limit + 1)]).intersection(poly)
    if ray.is_empty:
        return 0.0
    pieces = [g for g in getattr(ray, "geoms", [ray]) if g.geom_type == "LineString"]
    first = min(pieces, key=lambda g: g.distance(Point(p)), default=None)
    return float(first.length) if first is not None else 0.0


# ---------------------------------------------------------------- T-042 sacrificial layers


def sacrificial_layers(mesh: Mesh, profile: Profile) -> list[dict]:
    """A hole that starts at a ceiling (counterbore printed head-down, a hole above a pocket)
    leaves the slicer a ring to bridge, which it can't anchor. One thin layer across the hole
    turns the ring into a plain bridge; the hole is opened again with a drill or a pin."""
    lh = profile.layer_height
    out = []
    for zc, ceiling in ceilings(mesh, 2 * lh):
        above, below = _below(mesh, zc + 0.05), _below(mesh, zc - 0.05)
        for ring in ceiling.interiors:
            hole = Polygon(ring)
            d = 2 * np.sqrt(hole.area / np.pi)
            if d > MAX_SACRIFICIAL_D or d < 1.0:
                continue
            probe = hole.representative_point()
            if above.contains(probe) or below.contains(probe):
                continue  # solid continues there (a post under the ledge), not a hole
            disc = extrude(hole.buffer(OVERLAP, quad_segs=4), zc, zc + lh)
            if disc is not None:
                out.append({"z": zc, "diameter": d, "pieces": [disc], "bbox": hole.bounds})
    return out


# ---------------------------------------------------------------- T-041 thicker walls


def thicken_walls(mesh: Mesh, profile: Profile, lines: float, max_layers: int = 160) -> list[dict]:
    """Walls thinner than ``lines`` printed lines grow to that width (both sides), staying clear
    of other material so no gap fuses shut."""
    T = load_data("thresholds")
    lw, lh = profile.line_width, profile.layer_height
    target = lines * lw
    fuse = T["gaps"]["fuse_below_mm"]
    min_height = T["features"]["single_wall_min_height_mm"]
    layers = slice_mesh(mesh, lh, max_layers)
    tracker = Tracker(link=2 * lw)
    pieces: dict[int, list] = {}
    for layer in layers:
        g = layer.geom
        if g.is_empty:
            continue
        thin = _safe_diff(g, _opening(g, target / 2))
        for piece in parts(thin, max(lw * 1.5, 0.5)):
            w = _strip_width(piece)
            if w >= target * 0.95:
                continue
            region = tracker.add("thin", piece, layer, min_width=w)
            pieces.setdefault(id(region), []).append((layer.index, piece, w))
    height = float(mesh.triangles[..., 2].max())
    verts = np.unique(mesh.triangles.reshape(-1, 3), axis=0)
    out = []
    for region in tracker.of("thin"):
        if region.z1 - region.z0 < min_height:
            continue
        stack = []
        for i, piece, w in pieces[id(region)]:
            g = layers[i].geom
            grown = piece.buffer((target - w) / 2 + 0.02, quad_segs=QUAD, join_style="mitre", mitre_limit=2.0)
            other = _safe_diff(g, piece.buffer(0.05, quad_segs=QUAD))
            if not other.is_empty:
                grown = _safe_diff(grown, other.buffer(fuse + lw / 2, quad_segs=QUAD))
            stack.append((i, grown))
        slabs = _slabs(stack, layers, region, verts, height)
        if slabs:
            out.append({"z0": region.z0, "z1": region.z1, "width": region.metrics["min_width"], "target": target,
                        "pieces": slabs, "bbox": region.bounds})
    return out


def _slabs(stack, layers, region, verts, height) -> list[np.ndarray]:
    """Prisms for consecutive layers with the same grown outline. The first and last reach to the
    wall's real bottom and top (mesh vertices in its footprint), or one layer past it into the part."""
    groups: list[list] = []
    for i, g in stack:
        key = shapely.normalize(g.simplify(0.01))
        if groups and groups[-1][1] == i - 1 and key.equals_exact(groups[-1][2], 0.02):
            groups[-1][1] = i
        else:
            groups.append([i, i, key, g])
    first, last = stack[0][0], stack[-1][0]
    foot = shapely.union_all([g for _, g in stack]).buffer(0.1, quad_segs=QUAD)
    out = []
    for i0, i1, _, g in groups:
        z0 = (layers[i0 - 1].z + layers[i0].z) / 2 if i0 > 0 else 0.0
        z1 = (layers[i1].z + layers[i1 + 1].z) / 2 if i1 + 1 < len(layers) else height
        if i0 == first:
            z0 = _end_height(verts, foot, layers[i0 - 1].z if i0 > 0 else 0.0, layers[i0].z, low=True)
        if i1 == last:
            z1 = _end_height(verts, foot, layers[i1].z, layers[i1 + 1].z if i1 + 1 < len(layers) else height, low=False)
        for poly in parts(g, 0.01):
            prism = extrude(poly, z0, z1)
            if prism is not None:
                out.append(prism)
    return out


def _end_height(verts, foot, lo: float, hi: float, low: bool) -> float:
    """Where the wall really starts (low) or ends: the lowest/highest mesh vertex in its footprint
    between two slices. None found: the wall runs into solid there, so overlap it by a slice."""
    near = verts[(verts[:, 2] >= lo) & (verts[:, 2] <= hi)]
    if len(near):
        inside = near[shapely.contains_xy(foot, near[:, 0], near[:, 1])]
        if len(inside):
            return float(inside[:, 2].min() if low else inside[:, 2].max())
    return lo if low else hi
