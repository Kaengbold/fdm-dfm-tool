"""Printability checks on a sliced, oriented part.

The question every check answers: *will the printed part differ from the CAD model,
and how?* Supports and overhangs within the material's limits are normal and not
reported on their own; what matters is what they do to the part.

Each finding: ``{rule, impact, severity, detail, bbox, metrics}``.
``impact`` groups findings for the user:
  fails   — the print (or this feature) will likely fail
  shape   — prints, but shape or size differs from CAD (warps, shrinks, sags, closes up)
  surface — prints, but the surface is rough or marked
  weak    — prints, but breaks more easily than the CAD suggests
  info    — background (stability, handling)
``severity`` (problem / warn / info) is how likely or how bad it is.
Explanations and fixes live in ``data/rules.yaml``; thresholds in ``data/thresholds.yaml``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import shapely
from shapely.geometry import MultiPolygon, Polygon

from .heat import layer_heat
from .holes import horizontal_holes
from .mesh import Mesh
from .profile import Profile, load_data
from .progress import report
from .recommend import goal_plan
from .slicer import Layer, parts, slice_mesh

QUAD = 2  # buffer resolution; coarse is fine and much faster

RULE_IMPACT = {
    "exceeds_build_volume": "fails",
    "small_bed_contact": "fails",
    "tipping_risk": "fails",
    "too_thin_to_print": "fails",
    "hole_closes": "fails",
    "gap_fuses": "fails",
    "flat_gap_fuses": "fails",
    "trapped_support": "fails",
    "hardened_nozzle_required": "fails",
    "warping": "shape",
    "elephant_foot": "shape",
    "first_layer_detail": "shape",
    "small_hole_undersized": "shape",
    "nut_trap_tight": "shape",
    "insert_wall_thin": "fails",
    "screw_wall_thin": "weak",
    "bridge_sag": "shape",
    "hole_starts_midair": "shape",
    "sideways_hole_oval": "shape",
    "long_bridge": "shape",
    "small_layers_cooling": "shape",
    "tall_thin_feature": "shape",
    "support_marks": "surface",
    "fine_detail_rough": "surface",
    "shallow_relief": "surface",
    "side_detail_small": "surface",
    "too_hot_in_use": "weak",
    "dry_filament": "surface",
    "weak_upright_pin": "weak",
    "thin_neck": "weak",
    "single_line_wall": "weak",
    "enclosure_recommended": "shape",
    "turned_to_fit": "info",
    "shrinkage_size_error": "shape",
    "living_hinge": "weak",
    "snap_fit_upright": "weak",
    "thick_solid_block": "info",
    "pause_to_insert": "info",
    "shallow_slope_stairs": "surface",
    "coarse_facets": "shape",
    "vase_mode_possible": "info",
    "setup_layer_too_thick": "weak",
    "setup_support_gap_zero": "surface",
    "setup_flow_too_high": "fails",
    "setup_plate_too_sticky": "fails",
    "modelled_thread": "shape",
}
IMPACT_ORDER = ["fails", "shape", "surface", "weak", "info"]
SEVERITY_ORDER = {"problem": 0, "warn": 1, "info": 2}
# Findings that depend on the nozzle size (used to compare nozzles).
NOZZLE_RULES = {"too_thin_to_print", "fine_detail_rough", "single_line_wall", "hole_closes", "gap_fuses", "first_layer_detail"}


@dataclass
class Region:
    """A 2D issue tracked across consecutive analysed layers (a 3D blob)."""

    kind: str
    last: Polygon
    last_index: int
    z0: float
    z1: float
    bounds: list[float]  # minx, miny, maxx, maxy
    volume: float = 0.0
    metrics: dict = field(default_factory=dict)

    def add(self, piece: Polygon, layer: Layer):
        self.last, self.last_index, self.z1 = piece, layer.index, layer.z
        b = piece.bounds
        self.bounds = [min(self.bounds[0], b[0]), min(self.bounds[1], b[1]), max(self.bounds[2], b[2]), max(self.bounds[3], b[3])]
        self.volume += piece.area * layer.dz

    def bbox(self, layer_height: float) -> list[list[float]]:
        x0, y0, x1, y1 = self.bounds
        return [[x0, y0, max(self.z0 - layer_height / 2, 0.0)], [x1, y1, self.z1 + layer_height / 2]]


class Tracker:
    """Links 2D pieces on consecutive layers into regions. Metrics keep the max, or the
    min for keys starting with ``min_``."""

    def __init__(self, link: float):
        self.link = link
        self.regions: list[Region] = []

    def add(self, kind: str, piece: Polygon, layer: Layer, **metrics) -> Region:
        for r in self.regions:
            if r.kind == kind and r.last_index == layer.index - 1 and r.last.distance(piece) <= self.link:
                r.add(piece, layer)
                for k, v in metrics.items():
                    pick = min if k.startswith("min_") else max
                    r.metrics[k] = pick(r.metrics.get(k, v), v)
                return r
        r = Region(kind, piece, layer.index, layer.z, layer.z, list(piece.bounds), piece.area * layer.dz, dict(metrics))
        self.regions.append(r)
        return r

    def of(self, kind: str) -> list[Region]:
        return [r for r in self.regions if r.kind == kind]


def orient(mesh: Mesh, rotation: np.ndarray) -> Mesh:
    """Rotate into print orientation, centre on the bed origin, lowest point at z = 0."""
    t = mesh.triangles @ np.asarray(rotation).T
    lo, hi = t.reshape(-1, 3).min(axis=0), t.reshape(-1, 3).max(axis=0)
    return Mesh(t - [(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, lo[2]])


def run_checks(mesh: Mesh, profile: Profile, max_layers: int = 160, sliced: dict | None = None) -> dict:
    """Slice an oriented mesh and run all checks. Returns findings + stats. ``sliced``: real layer
    times and plastic from the G-code (gcode.layer_times) for the heat check."""
    T = load_data("thresholds")
    layers = slice_mesh(mesh, profile.layer_height, max_layers)
    tracker = Tracker(link=2 * profile.line_width)
    _scan_layers(layers, profile, tracker, T)

    findings: list[dict] = []
    findings += _support_findings(layers, tracker, profile, T)
    findings += _feature_findings(tracker, profile, T)
    findings += _hole_findings(tracker, profile, T)
    findings += _fastener_findings(tracker, profile, T)
    findings = _sideways_hole_findings(mesh, profile, T, findings)
    findings += _first_layer_findings(layers, profile, T)
    findings += _bed_findings(layers, profile, T)
    findings += _weak_findings(layers, tracker, profile, T)
    findings += _flex_findings(layers, profile, T)
    stats = _estimate(layers, profile)
    heat = _layer_heat(layers, profile, sliced)
    findings += _cooling_findings(layers, heat, profile, T, sliced)
    findings += _profile_findings(layers, profile)
    findings += _shrinkage_findings(mesh, profile, T)
    findings += pocket_findings(mesh, profile, T)
    findings += slope_findings(mesh, profile, T)
    findings += facet_findings(mesh, T)
    findings += _relief_findings(mesh, profile, T)
    findings += flat_gap_findings(mesh, profile, T)
    findings += _side_relief_findings(mesh, profile, T)
    findings += _use_heat_findings(profile, T)

    for f in findings:
        f["impact"] = RULE_IMPACT.get(f["rule"], "info")
    findings.sort(key=lambda f: (IMPACT_ORDER.index(f["impact"]), SEVERITY_ORDER[f["severity"]]))
    del stats["layer_times"]
    findings += vase_findings(layers, tracker, profile, T, stats)
    normals, areas = mesh.normals_and_areas()
    z = mesh.triangles[..., 2]
    top = (normals[:, 2] > 0.999) & (z.mean(axis=1) > z.max() - profile.layer_height / 2)
    stats.update(
        top_flat_mm2=float(areas[top].sum()),
        round_share=_round_share(layers, T["seam"]["round_min"]),
        layer_heat={"z": [l.z for l in layers], "temp_c": heat.round(1).tolist(), "softening_c": profile.material["softening_c"]},
        layers_analysed=len(layers),
        height_mm=float(mesh.triangles[..., 2].max()),
        support_area_mm2=float(sum(r.metrics.get("area", 0) for r in tracker.of("overhang"))),
        needs_support=any(r.volume >= 2.0 for r in tracker.of("overhang")),
    )
    return {"findings": findings, "stats": stats, "z_strength_factor": profile.z_strength_factor, "material": profile.material["name"]}


# ---------------------------------------------------------------- layer scan


# A layer may reach this far past the overhang angle: slicing noise on walls and curves, not a real
# overhang. (Was half a line width, which let PLA's 55° pass up to ~68° at 0.2 mm layers, T-172.)
OVERHANG_TOL_MM = 0.05
# Smallest overhang strip per layer: just past the angle limit, each layer only sticks out a little
# along a long edge. Specks are dropped later by volume (_support_findings).
OVERHANG_MIN_PIECE_MM2 = 0.25


def _scan_layers(layers: list[Layer], profile: Profile, tracker: Tracker, T: dict):
    lw, nozzle = profile.line_width, float(profile.nozzle)
    tan_lim = np.tan(np.radians(profile.material["max_overhang_deg"]))
    max_bridge = profile.material["max_bridge_mm"]
    min_piece = max(1.0, 4 * lw * lw)
    F, G, H = T["features"], T["gaps"], T["holes"]
    vanish, rough = F["vanish_below_nozzle"] * nozzle, F["rough_below_nozzle"] * nozzle
    single = F["single_wall_below_nozzle"] * nozzle
    prev = cached = None
    for i, layer in enumerate(layers):
        if i % 8 == 0:
            report(0.9 * i / len(layers), "Checking layers")
        g = layer.geom
        if g.is_empty:
            prev = g
            continue
        # Prismatic parts repeat the same cross-section for many layers. The same outline may start at
        # another vertex or carry extra collinear points from layer to layer, so compare a normalised copy.
        key = shapely.normalize(g.simplify(0.001))
        same = cached is not None and key.equals_exact(cached[0], 1e-4)
        # Areas that need support: reach further out than the overhang limit allows.
        # (Nothing can overhang when the layer equals the one below.)
        if prev is not None and not prev.is_empty and not (same and prev is cached[2]):
            reach = layer.dz * tan_lim + OVERHANG_TOL_MM
            anchor_zone = prev.buffer(reach + lw, quad_segs=QUAD)  # must reach past the supported zone
            supported = prev.buffer(reach, quad_segs=QUAD)
            held_zone = prev.buffer(reach + OVERHANG_TOL_MM, quad_segs=QUAD)
            for piece in parts(_safe_diff(g, supported), OVERHANG_MIN_PIECE_MM2):
                span, anchors = _bridge_span(piece, anchor_zone, max_bridge, held_zone)
                holes = _open_holes(piece, g)
                if holes:
                    tracker.add("hole_start", piece, layer, hole_d=max(holes))
                if anchors >= 2:
                    tracker.add("bridge", piece, layer, span=span, area=piece.area)
                else:
                    tracker.add("overhang", piece, layer, area=piece.area, enclosed=float(_is_enclosed(piece, g)))
        # Width/hole/pin analysis only depends on this layer's shape: reuse it while the shape repeats.
        features = cached[1] if same else _layer_features(g, lw, vanish, rough, single, G, H, T["pins"], T["fasteners"])
        cached = (key if not same else cached[0], features, g)
        for kind, piece, metrics in features:
            tracker.add(kind, piece, layer, **metrics)
        prev = g


def _layer_features(g, lw, vanish, rough, single, G, H, P, F) -> list[tuple]:
    """(kind, piece, metrics) for one layer: thin features, narrow gaps, round and hexagonal holes, slender islands."""
    out = []
    # Positive features by width: vanish < rough < single-wall (morphological openings).
    too_thin = _safe_diff(g, _opening(g, vanish / 2))
    below_rough = _safe_diff(g, _opening(g, rough / 2))
    thin = _safe_diff(below_rough, too_thin.buffer(0.01))
    wall = _safe_diff(_safe_diff(g, _opening(g, single / 2)), below_rough.buffer(0.01))
    for kind, geom in (("too_thin", too_thin), ("rough", thin), ("single_wall", wall)):
        out += [(kind, piece, {"width": _strip_width(piece)}) for piece in parts(geom, max(lw * 1.5, 0.5))]
    # Negative features: slots/clearances that a closing with radius r fills are narrower than 2r.
    for piece in parts(_safe_diff(_closing(g, G["fuse_below_mm"] / 2), g), 0.05):
        if _strip_width(piece) > 0.02:
            out.append(("gap", piece, {"width": _strip_width(piece)}))
    # Round vertical holes (interior rings), for size checks; hexagonal ones are nut traps.
    # Holes sized for an insert or for a screw cut into the plastic: how much wall is around them.
    sized = [(f["insert_hole_mm"], F["insert_match_mm"]) for f in F.values() if isinstance(f, dict)]
    sized += [(f["pilot_mm"], F["pilot_match_mm"]) for f in F.values() if isinstance(f, dict)]
    for poly in parts(g):
        for i, ring in enumerate(poly.interiors):
            hole = Polygon(ring)
            af = _hex_across_flats(hole)
            if af is not None:
                out.append(("hex", hole, {"min_af": af}))
                continue
            d = 2 * np.sqrt(hole.area / np.pi)
            if d <= H["undersize_up_to_mm"] and 4 * np.pi * hole.area / max(hole.length, 1e-9) ** 2 >= H["circularity_min"]:
                metrics = {"min_d": d}
                if any(abs(d - x) <= tol for x, tol in sized):
                    others = [poly.exterior] + [r for j, r in enumerate(poly.interiors) if j != i]
                    metrics["min_wall"] = min(ring.distance(o) for o in others)
                out.append(("hole", hole, metrics))
    # Slender islands: pins/posts.
    for island in parts(g, 0.01):
        w, length = _rect_sides(island)
        if w < P["max_width_mm"] and length < 2 * P["max_width_mm"]:
            out.append(("slender", island, {"width": w, "length": length}))
    return out


def _open_holes(piece: Polygon, layer_geom) -> list[float]:
    """Diameters of holes in an unsupported piece that stay open in the layer (a hole that starts in
    mid-air, e.g. above a counterbore). Holes filled in the layer are posts below the roof instead."""
    out = []
    for ring in piece.interiors:
        hole = Polygon(ring)
        d = 2 * np.sqrt(hole.area / np.pi)
        if 1.0 <= d <= 25.0 and not layer_geom.contains(hole.representative_point()):
            out.append(float(d))
    return out


def _is_enclosed(piece: Polygon, layer_geom) -> bool:
    """True when the piece touches no outside edge of its layer (a roof inside a cavity)."""
    outer = shapely.union_all([Polygon(p.exterior) for p in parts(layer_geom)]).boundary
    return piece.boundary.distance(outer) > 0.05


def _bridge_span(piece: Polygon, anchor_zone, max_span: float = np.inf, held_zone=None) -> tuple[float, int]:
    """Number of separate anchors on the layer below, and the largest gap between them.

    A piece held all the way round (roof of a pocket, ring over a counterbore) is one closed anchor;
    it bridges across its narrow width, so it counts as two anchors that far apart when that width is
    at most ``max_span`` (wider, the slicer supports it instead). ``held_zone`` (the layer below, barely
    grown) decides "all the way round": a strip along an edge that is narrower than ``anchor_zone``'s
    margin lies inside it entirely, but its outer edge is free — an overhang, not a bridge (T-172)."""
    touch = piece.boundary.intersection(anchor_zone)
    if touch.is_empty:
        return 0.0, 0
    merged = shapely.line_merge(touch) if touch.geom_type == "MultiLineString" else touch
    anchors = [a for a in getattr(merged, "geoms", [merged]) if a.length > 0.05]
    if len(anchors) < 2:
        held = anchor_zone if held_zone is None else held_zone
        if anchors and piece.exterior.intersection(held).length >= 0.9 * piece.exterior.length:
            width = _min_width(Polygon(piece.exterior))
            if width <= max_span:
                return width, 2
        return 0.0, len(anchors)
    span = max(min(a.distance(b) for b in anchors if b is not a) for a in anchors)
    return float(span), len(anchors)


OVERLAY_GRID = 1e-4  # mm: precision of the robust overlay below, far finer than anything printed


def _safe_diff(a, b):
    """a − b with GEOS's robust overlay (snap-rounded to ``OVERLAY_GRID``). The morphological checks
    subtract a shape from its own opening or closing, whose outlines run almost on top of each other:
    the plain overlay of Pyodide's older GEOS then throws a TopologyException that try/except can't
    catch in the browser (it kills the engine). Seen on a part with a fill and a chamfer crossing (T-201)."""
    if a.is_empty or b.is_empty:
        return a
    return _safe_overlay(shapely.difference, a, b)


def _safe_overlay(op, a, b):
    """``op`` (shapely.difference / intersection / union) of two areas with the robust overlay, see
    ``_safe_diff``; always an area back (empty if nothing works)."""
    try:
        out = op(a, b, grid_size=OVERLAY_GRID)
        # Snap rounding can collapse a hair-thin piece into a line; a mixed result can't go into the next overlay.
        if out.geom_type in ("Polygon", "MultiPolygon"):
            return out
        return MultiPolygon([q for g in shapely.get_parts(out) for q in shapely.get_parts(g) if q.geom_type == "Polygon"])
    except Exception:
        try:
            return op(shapely.set_precision(a, 1e-3), shapely.set_precision(b, 1e-3))
        except Exception:
            try:
                return op(shapely.make_valid(a), shapely.make_valid(b))
            except Exception:
                return Polygon()


def _opening(g, r: float):
    """Morphological opening: removes everything narrower than 2r."""
    b = g.buffer(-r, quad_segs=QUAD)
    if b.is_empty:
        return Polygon()
    return b.buffer(r * 1.05, quad_segs=QUAD)


def _thick_core(g, r: float, cells: float = 4.0) -> tuple[float, tuple | None]:
    """Area and bounds of the part of ``g`` thicker than 2r all round (its morphological opening), on
    a grid of r / ``cells`` — without a buffer. Pyodide's GEOS 3.12 throws "depth mismatch" on large
    shrinking buffers of some plain outlines (T-268: an L-shaped layer 2–6 mm thick, a rounded square
    22 mm wide, both shrunk by 8 mm), which kills the engine; no buffer setting avoided it for both.
    Point-in-polygon tests and distances can't throw that. Bounds None: nothing that thick."""
    h = r / cells
    x0, y0, x1, y1 = g.bounds
    X, Y = np.meshgrid(np.arange(x0 + h / 2, x1, h), np.arange(y0 + h / 2, y1, h))
    inside = shapely.contains_xy(g, X, Y)
    deep = np.zeros_like(inside)
    deep[inside] = shapely.distance(g.boundary, shapely.points(X[inside], Y[inside])) >= r
    if not deep.any():
        return 0.0, None
    # Grow the deep cells back by r (a disc of cells): together they are the opening.
    k = int(np.ceil(cells))
    padded = np.pad(deep, k)
    opened = np.zeros_like(deep)
    for dy in range(-k, k + 1):
        for dx in range(-k, k + 1):
            if dx * dx + dy * dy <= cells * cells:
                opened |= padded[k + dy:k + dy + deep.shape[0], k + dx:k + dx + deep.shape[1]]
    opened &= inside
    xs, ys = X[opened], Y[opened]
    return float(opened.sum() * h * h), (xs.min() - h / 2, ys.min() - h / 2, xs.max() + h / 2, ys.max() + h / 2)


def _closing(g, r: float):
    """Morphological closing: fills gaps narrower than 2r."""
    b = g.buffer(r, quad_segs=QUAD)
    if b.is_empty:
        return Polygon()
    return b.buffer(-r * 1.05, quad_segs=QUAD)


def _strip_width(piece: Polygon) -> float:
    """Width of a strip-like piece: A = wL and P ≈ 2L."""
    return 2 * piece.area / max(piece.length, 1e-9)


def _rect_sides(poly: Polygon) -> tuple[float, float]:
    """Short and long side of the minimum rotated bounding rectangle."""
    rect = poly.minimum_rotated_rectangle
    if rect.geom_type != "Polygon":
        return 0.0, poly.length / 2
    c = np.asarray(rect.exterior.coords)
    a, b = np.linalg.norm(c[1] - c[0]), np.linalg.norm(c[2] - c[1])
    return float(min(a, b)), float(max(a, b))


def _min_width(poly: Polygon) -> float:
    return _rect_sides(poly)[0]


def _finding(rule, severity, detail, region: Region | None = None, profile: Profile | None = None, bbox=None, **metrics):
    if region is not None:
        bbox = region.bbox(profile.layer_height)
        metrics = {**{k: round(v, 2) for k, v in region.metrics.items()}, **metrics}
    return {"rule": rule, "severity": severity, "detail": detail, "bbox": bbox, "metrics": metrics}


def _zrange(r: Region) -> str:
    return f"{r.z0:.1f} mm" if r.z1 - r.z0 < 0.05 else f"{r.z0:.1f}–{r.z1:.1f} mm"


def _layer_bbox(geom, z0: float, z1: float):
    b = geom.bounds
    return [[b[0], b[1], z0], [b[2], b[3], z1]]


# ---------------------------------------------------------------- supports & bridges


def _support_findings(layers, tracker: Tracker, profile: Profile, T: dict) -> list[dict]:
    """Supports are normal; report what they do to the part: marks, or support that can't come out."""
    out = []
    B = T["bridges"]
    for r in tracker.of("overhang"):
        if r.volume < 2.0:  # specks from round edges
            continue
        if r.metrics.get("enclosed") and _cavity_is_closed(r, layers):
            out.append(_finding("trapped_support", "warn",
                                f"Overhang inside a closed cavity at {_zrange(r)} — support printed there can't be removed", r, profile))
            continue
        pocket = None if r.metrics.get("enclosed") else _side_pocket(r, layers)
        if pocket and pocket[0] >= B["pocket_depth_mm"] and pocket[0] >= B["pocket_ratio"] * pocket[1]:
            depth, mouth = pocket
            out.append(_finding("trapped_support", "warn",
                                f"Support under the roof at {_zrange(r)} sits in a pocket {depth:.0f} mm deep behind a {mouth:.0f} mm "
                                "opening — hard to reach and pull out", r, profile, depth_mm=round(depth, 1), opening_mm=round(mouth, 1)))
            continue
        sev = "info" if profile.multi_material else "warn"
        out.append(_finding("support_marks", sev,
                            f"Supported underside at {_zrange(r)} (~{r.metrics['area']:.0f} mm²) will be rougher than the rest", r, profile))
    for r in tracker.of("hole_start"):
        d = r.metrics["hole_d"]
        out.append(_finding("hole_starts_midair", "warn",
                            f"Ø{d:.1f} mm hole starts in mid-air at {r.z0:.1f} mm (e.g. above a counterbore) — the ring around it "
                            "has no edge to bridge from, so that layer droops and the hole's start prints messy", r, profile))
    max_bridge = profile.material["max_bridge_mm"]
    for r in tracker.of("bridge"):
        span = r.metrics["span"]
        if span > max_bridge:
            out.append(_finding("long_bridge", "warn",
                                f"{span:.0f} mm bridge at {r.z0:.1f} mm will sag (comfortable for {profile.material['name']}: ≤ {max_bridge} mm)", r, profile))
        elif span >= B["sag_from_mm"]:
            lo, hi = B["sag_mm"] if span < B["large_sag_from_mm"] else (0.4, 1.0)
            out.append(_finding("bridge_sag", "info",
                                f"{span:.0f} mm wide hole/slot top at {r.z0:.1f} mm sags about {lo}–{hi} mm", r, profile))
    return out


def _cavity_is_closed(r: Region, layers: list[Layer]) -> bool:
    """Walk down from an enclosed roof: closed when the void below ends in a floor before the bed
    and never opens to the side."""
    roof = Polygon(r.last.exterior) if r.last.geom_type == "Polygon" else r.last
    for layer in reversed(layers[: r.last_index]):
        if layer.z >= r.z0:
            continue
        filled = shapely.union_all([Polygon(p.exterior) for p in parts(layer.geom)]) if not layer.geom.is_empty else Polygon()
        void = _safe_diff(roof, layer.geom)
        if void.area < 0.05 * roof.area:
            return True  # reached a floor
        if _safe_diff(void, filled).area > 0.05 * void.area:
            return False  # opens to the outside
    return False  # open to the bed


def _side_pocket(r: Region, layers: list[Layer]) -> tuple[float, float] | None:
    """(depth, opening width) when the space under a roof is a pocket with a floor, open only to
    one side: the part's outline (convex hull) closes it except for the opening."""
    roof = Polygon(r.last.exterior) if r.last.geom_type == "Polygon" else r.last
    below = [layer for layer in layers[: r.last_index] if layer.z < r.z0 and not layer.geom.is_empty]
    if not below:
        return None
    g = below[-1].geom
    hull = g.convex_hull
    pocket = shapely.intersection(_safe_diff(roof, g), hull)
    if pocket.is_empty or pocket.area < 0.3 * roof.area:
        return None
    mouth = pocket.boundary.intersection(hull.boundary.buffer(0.05, quad_segs=QUAD))
    if mouth.is_empty or mouth.length < 0.5:
        return None
    # It needs a floor: a pocket that reaches the bed can be cleared from below.
    for layer in reversed(below):
        if _safe_diff(pocket, layer.geom).area < 0.05 * pocket.area:
            break
    else:
        return None
    pts = shapely.points(shapely.get_coordinates(pocket))
    depth = float(shapely.distance(pts, mouth).max())
    return depth, float(mouth.length)


# ---------------------------------------------------------------- feature size


def _feature_findings(tracker: Tracker, profile: Profile, T: dict) -> list[dict]:
    out = []
    nozzle = float(profile.nozzle)
    F = T["features"]
    # Thin strips seen on only one or two slices are the tips of slopes meeting a wall (chamfers,
    # gussets, fillets): they merge into the wall when printed.
    for r in tracker.of("too_thin"):
        if r.z1 - r.z0 < 2 * profile.layer_height:
            continue
        out.append(_finding("too_thin_to_print", "problem",
                            f"About {r.metrics['width']:.2f} mm thin at {_zrange(r)} — too thin for a {profile.nozzle} mm nozzle "
                            f"(needs more than ~{F['vanish_below_nozzle'] * nozzle:.2f} mm), it won't print", r, profile))
    for r in tracker.of("rough"):
        if r.volume < 1.0 or r.z1 - r.z0 < 2 * profile.layer_height:
            continue
        out.append(_finding("fine_detail_rough", "info",
                            f"Detail about {r.metrics['width']:.2f} mm wide at {_zrange(r)} prints rough or incomplete "
                            f"(under {F['rough_below_nozzle'] * nozzle:.2f} mm)", r, profile))
    for r in tracker.of("single_wall"):
        if r.z1 - r.z0 < F["single_wall_min_height_mm"]:
            continue
        out.append(_finding("single_line_wall", "info",
                            f"Wall about {r.metrics['width']:.2f} mm thick, {r.z1 - r.z0:.0f} mm tall — only one line of plastic, easy to break", r, profile))
    fuse = T["gaps"]["fuse_below_mm"]
    for r in tracker.of("gap"):
        if r.z1 - r.z0 < 2 * profile.layer_height and r.volume < 0.2:
            continue
        out.append(_finding("gap_fuses", "problem",
                            f"Gap about {r.metrics['width']:.2f} mm at {_zrange(r)} — narrower than {fuse} mm fuses shut", r, profile))
    return out


def lost_detail(layers: list[Layer], nozzle: float) -> dict:
    """How much of the part a nozzle can't trace (mm³): pieces narrower than the features thresholds
    drop out of each layer's opening. ``lost``: won't print at all; ``rough``: prints rough or
    incomplete; ``lost_at_mm``: the height that loses the most. Corner slivers (any outside corner
    rounds a little) are left out: only pieces bigger than r² count."""
    F = load_data("thresholds")["features"]
    radii = {"lost": F["vanish_below_nozzle"] * nozzle / 2, "rough": F["rough_below_nozzle"] * nozzle / 2}
    vol = dict.fromkeys(radii, 0.0)
    worst = (0.0, None)
    for layer in layers:
        if layer.geom.is_empty:
            continue
        for key, r in radii.items():
            gone = _safe_diff(layer.geom, _opening(layer.geom, r))
            area = sum(q.area for q in parts(gone) if q.area >= r * r)
            vol[key] += area * layer.dz
            if key == "lost" and area > worst[0]:
                worst = (area, layer.z)
    return {"lost_mm3": round(vol["lost"], 1), "rough_mm3": round(max(vol["rough"] - vol["lost"], 0.0), 1),
            "lost_at_mm": None if worst[1] is None else round(float(worst[1]), 1)}


def _hole_findings(tracker: Tracker, profile: Profile, T: dict) -> list[dict]:
    out = []
    H = T["holes"]
    nozzle = float(profile.nozzle)
    min_d = max(H["vertical_min_mm"] * nozzle / 0.4, H["vertical_min_nozzle"] * nozzle)
    lo, hi = H["typical_undersize_mm"]
    for r in tracker.of("hole"):
        if r.z1 - r.z0 < 3 * profile.layer_height:
            continue
        d = r.metrics["min_d"]
        if d < min_d:
            out.append(_finding("hole_closes", "problem",
                                f"Ø{d:.1f} mm hole at {_zrange(r)} — too small for a {profile.nozzle} mm nozzle (min ~{min_d:.1f} mm), it will close up", r, profile))
        else:
            out.append(_finding("small_hole_undersized", "warn",
                                f"Ø{d:.1f} mm hole prints about {lo}–{hi} mm too small — a screw or pin of that size won't fit without drilling"
                                + fastener_advice(d, T), r, profile, **_fastener(d, T)))
    return out


def _round_share(layers: list[Layer], round_min: float) -> float:
    """Share of layers whose outlines are all round (no corner to hide the seam in)."""
    shapes = [list(parts(l.geom, 0.5)) for l in layers if not l.geom.is_empty]
    if not shapes:
        return 0.0
    round_layers = [all(4 * np.pi * p.area / max(p.exterior.length, 1e-9) ** 2 >= round_min for p in ps) for ps in shapes if ps]
    return float(np.mean(round_layers)) if round_layers else 0.0


def _hex_across_flats(hole: Polygon) -> float | None:
    """Across-flats size of a regular hexagonal hole (a nut trap), None for other shapes."""
    c = np.asarray(hole.simplify(0.05).exterior.coords)[:-1]
    if len(c) != 6:
        return None
    sides = np.linalg.norm(np.roll(c, -1, axis=0) - c, axis=1)
    if sides.std() > 0.05 * sides.mean():
        return None
    return float(np.sqrt(2 * hole.area / np.sqrt(3)))


def _fastener_findings(tracker: Tracker, profile: Profile, T: dict) -> list[dict]:
    """Nut traps too tight for their nut, and heat-set insert holes with too little wall around them."""
    F = T["fasteners"]
    screws = {k: f for k, f in F.items() if isinstance(f, dict)}
    lo, hi = F["nut_match_mm"]
    out = []
    for r in tracker.of("hex"):
        af = r.metrics["min_af"]
        fits = [k for k, f in screws.items() if f["nut_af_mm"] + lo <= af <= f["nut_af_mm"] + hi]
        name = min(fits, key=lambda k: abs(screws[k]["nut_af_mm"] - af), default=None)
        if name is None or af >= screws[name]["nut_af_mm"] + F["nut_clearance_mm"]:
            continue
        want = screws[name]["nut_af_mm"] + F["nut_clearance_mm"] + 0.1
        out.append(_finding("nut_trap_tight", "warn",
                            f"Nut trap for an {name} nut at {_zrange(r)} is {af:.2f} mm across the flats — the nut is "
                            f"{screws[name]['nut_af_mm']} mm and printed holes come out small; make it about {want:.1f} mm",
                            r, profile, fastener=name, across_flats=round(af, 2)))
    for r in tracker.of("hole"):
        wall = r.metrics.get("min_wall")
        if wall is None or r.z1 - r.z0 < 3 * profile.layer_height:
            continue
        d = r.metrics["min_d"]
        pilot = min(screws, key=lambda k: abs(screws[k]["pilot_mm"] - d))
        if abs(screws[pilot]["pilot_mm"] - d) <= F["pilot_match_mm"] and wall < screws[pilot]["d"]:
            # Pilot sizes overlap other holes (an M4 pilot is an M3 clearance hole): a tip, worded as "if".
            out.append(_finding("screw_wall_thin", "info",
                                f"If the Ø{d:.1f} mm hole at {_zrange(r)} is for an {pilot} screw cut straight into the plastic: "
                                f"only {wall:.1f} mm of wall around it — aim for about {screws[pilot]['d']:g} mm (as thick as the screw)",
                                r, profile, fastener=pilot, wall_mm=round(wall, 2)))
        name = min(screws, key=lambda k: abs(screws[k]["insert_hole_mm"] - d))
        if abs(screws[name]["insert_hole_mm"] - d) > F["insert_match_mm"] or wall >= F["insert_min_wall_mm"]:
            continue
        out.append(_finding("insert_wall_thin", "warn",
                            f"If the Ø{d:.1f} mm hole at {_zrange(r)} is for an {name} heat-set insert: only {wall:.1f} mm of wall "
                            f"around it — the melted plastic pushes out and can split it (aim for {F['insert_min_wall_mm']}–2 mm)",
                            r, profile, fastener=name, wall_mm=round(wall, 2)))
    return out


def insert_pockets(mesh: Mesh, profile: Profile, T: dict | None = None) -> list[dict]:
    """Closed pockets inside the part (an inner shell) with a flat roof: room for a magnet, nut, bearing
    or weight put in while printing. Each: its box (print frame), the height to pause at (the top of the
    first roof layer, as slicers number them), that layer's number, and the roof's shortest span."""
    from .formats import _components

    P = (T or load_data("thresholds"))["pauses"]
    t = mesh.triangles
    labels = _components(t)
    n = int(labels.max()) + 1
    if n == 1:
        return []
    signed = np.einsum("ij,ij->i", t[:, 0], np.cross(t[:, 1], t[:, 2])) / 6.0
    vol = np.bincount(labels, signed, n)
    if vol.sum() < 0:  # every shell inside out (inverted STL): cavities are then the positive ones
        signed, vol = -signed, -vol
    normals, areas = mesh.normals_and_areas()
    lh = profile.layer_height
    out = []
    for c in np.flatnonzero(vol < -P["min_pocket_mm3"]):
        tri = t[labels == c]
        lo, hi = tri.reshape(-1, 3).min(axis=0), tri.reshape(-1, 3).max(axis=0)
        # The roof: faces at the top of the pocket, level (an inner shell's normals point into the pocket).
        top = (np.abs(tri[:, :, 2] - hi[2]).max(axis=1) < 0.05) & (np.abs(normals[labels == c][:, 2]) > 0.99)
        footprint = (hi[0] - lo[0]) * (hi[1] - lo[1])
        if areas[labels == c][top].sum() < P["roof_flat_share"] * footprint:
            continue
        layer = int(np.floor(hi[2] / lh + 1e-6)) + 1  # the first layer printed over the pocket
        out.append({"lo": lo.tolist(), "hi": hi.tolist(), "pause_z": round(layer * lh, 3), "layer": layer, "top_z": round(float(hi[2]), 3),
                    "span_mm": float(min(hi[0] - lo[0], hi[1] - lo[1])), "volume_mm3": float(-vol[c])})
    return out


def pocket_findings(mesh: Mesh, profile: Profile, T: dict) -> list[dict]:
    """A pause to put something into each closed pocket (pause_to_insert)."""
    out = []
    for p in insert_pockets(mesh, profile, T):
        lo, hi = p["lo"], p["hi"]
        size = f"{hi[0] - lo[0]:.1f} × {hi[1] - lo[1]:.1f} × {hi[2] - lo[2]:.1f} mm"
        text = (f"Closed pocket ({size}) at {lo[2]:.1f}–{hi[2]:.1f} mm: to put a magnet, nut or weight in, pause the print "
                f"before layer {p['layer']} (at {p['pause_z']:.2f} mm), drop it in, and carry on. The 3MF can carry the pause "
                "for OrcaSlicer and PrusaSlicer; for Bambu Studio, add it on that layer in the preview, or let this app add it to "
                "the file you sliced")
        long = p["span_mm"] > profile.material["max_bridge_mm"]
        if long:
            text += (f". The roof spans {p['span_mm']:.0f} mm, longer than {profile.material['name']} bridges cleanly: "
                     "it may sag onto what you put in")
        out.append(_finding("pause_to_insert", "warn" if long else "info", text, bbox=[lo, hi],
                            pause_z=p["pause_z"], layer=p["layer"], top_z=p["top_z"], span_mm=round(p["span_mm"], 1)))
    return out


SPEED_KEYS = {"outer_wall_speed": "outer wall", "inner_wall_speed": "inner wall", "sparse_infill_speed": "infill",
              "internal_solid_infill_speed": "solid infill", "top_surface_speed": "top surface"}


def setup_findings(profile: Profile, settings: dict | None = None, T: dict | None = None) -> list[dict]:
    """Mistakes in the printer / material presets and the slicer values the user set (``settings``, slicer
    keys): no part needed, so the app can say it when a preset is saved and before the 3MF goes out."""
    S = (T or load_data("thresholds"))["setup"]
    settings = settings or {}
    num = lambda k, d=None: float(settings[k]) if str(settings.get(k, "")).replace(".", "", 1).isdigit() else d  # noqa: E731
    mat, prn = profile.material, profile.printer
    nozzle = float(profile.nozzle)
    lh = num("layer_height", goal_plan(profile)["layer_height"])
    lw = num("line_width", profile.line_width)
    out = []
    if lh > S["max_layer_share"] * nozzle + 1e-9:
        out.append(_finding("setup_layer_too_thick", "warn", f"Layer height {lh:.2f} mm on a {nozzle:g} mm nozzle is more than "
                            f"{S['max_layer_share']:.0%} of it: the layers are pressed on too little and bond badly. Use at most "
                            f"{S['max_layer_share'] * nozzle:.2f} mm, or a bigger nozzle", layer_height=lh, nozzle=nozzle))
    gap = num("support_top_z_distance")
    if gap is not None and gap < S["min_support_gap_mm"] and not profile.multi_material:  # a support filament may touch
        out.append(_finding("setup_support_gap_zero", "warn", f"Support top gap {gap:.2f} mm with the same filament: the support welds "
                            "onto the part and tears the surface when you remove it. Use 0.15–0.2 mm (PETG 0.2–0.25 mm)", gap_mm=gap))
    flow = float(mat["max_flow_mm3s"])
    fast = [(label, num(k)) for k, label in SPEED_KEYS.items() if num(k)]
    for label, v in fast:
        if v * lw * lh > flow * 1.05:
            out.append(_finding("setup_flow_too_high", "info", f"{label.capitalize()} speed {v:.0f} mm/s needs {v * lw * lh:.0f} mm³/s of melted "
                                f"plastic; {mat['name']} melts about {flow:.0f} mm³/s, so the slicer slows it down to "
                                f"{flow / (lw * lh):.0f} mm/s (as long as its max volumetric speed is set)", speed=v, flow_mm3s=round(v * lw * lh, 1)))
            break
    if flow > S["standard_hotend_flow_mm3s"]:
        out.append(_finding("setup_flow_too_high", "warn", f"{mat['name']} is set to melt {flow:.0f} mm³/s: more than a standard hotend does "
                            f"(about 11–22 mm³/s for PLA). Unless the printer has a high-flow hotend, it under-extrudes and the walls "
                            "come out thin and weak", flow_mm3s=flow))
    sticky = any(k in " ".join([mat.get("name", ""), profile.material_id, mat.get("based_on", "")]).lower() for k in S["sticky_on_smooth"])
    if sticky and prn.get("bed_surface") in ("smooth_pei", "glass"):
        plate = "smooth PEI" if prn["bed_surface"] == "smooth_pei" else "bare glass"
        out.append(_finding("setup_plate_too_sticky", "warn", f"{mat['name']} on {plate} bonds so hard it can tear pieces out of the "
                            "plate: put glue stick on as a release layer, or use a textured plate", surface=prn["bed_surface"]))
    if mat.get("abrasive") and not prn.get("hardened_nozzle"):
        out.append(_finding("hardened_nozzle_required", "problem",
                            f"{mat['name']} wears out the standard {prn['name']} nozzle — fit a hardened one first"))
    return out


def vase_findings(layers: list[Layer], tracker: Tracker, profile: Profile, T: dict, stats: dict) -> list[dict]:
    """An open container that could print in spiral vase mode (vase_mode_possible), with the time saved."""
    V = T["vase"]
    if not layers or layers[-1].z < V["min_height_mm"] or any(r.volume >= 2.0 for r in tracker.of("overhang")):
        return []
    pieces = [parts(layer.geom, 0.5) for layer in layers]
    if any(len(p) != 1 for p in pieces[1:]):  # islands (several outlines) or an empty layer
        return []
    holes = [len(p[0].interiors) for p in pieces]
    if holes[-1] != 1 or max(holes) > 1:  # open at the top, nothing else hollow
        return []
    first = holes.index(1)
    if first == 0 or any(h != 1 for h in holes[first:]) or layers[first].z > (1 - V["min_open_share"]) * layers[-1].z:
        return []  # open at the bottom too (a tube: vase mode would close it), or mostly solid
    lh, lw = goal_plan(profile)["layer_height"], profile.line_width
    v = min(profile.speeds["outer_wall"] * TIME_EFFICIENCY, profile.material["max_flow_mm3s"] / (lw * lh))
    spiral = sum(p[0].exterior.length * layer.dz / lh for p, layer in zip(pieces[first:], layers[first:])) / v / 60
    floor = stats["print_time_min"] * layers[first].z / layers[-1].z  # the solid bottom, roughly as now
    vase_min = START_MIN + floor + spiral
    if vase_min > (1 - V["min_saving"]) * stats["print_time_min"]:
        return []
    ring = [p[0] for p in pieces[first:]]
    wall = float(np.median([r.area / max(r.exterior.length, 1e-9) for r in ring]))
    if wall > V["max_wall_mm"]:  # a thick-walled part, not a container: one line would be a different part
        return []
    text = (f"Open container: it can print in spiral vase mode — one continuous wall with no seam, about "
            f"{vase_min:.0f} min instead of {stats['print_time_min']:.0f}. The wall is then one line ({lw:.2f} mm"
            + (f", your model has {wall:.1f} mm" if wall > 1.5 * lw else "") + "), with no top and no infill: weaker, "
            "and not watertight under pressure")
    lo = np.array(ring[0].bounds[:2])
    hi = np.array(ring[0].bounds[2:])
    return [_finding("vase_mode_possible", "info", text, bbox=[[*lo.tolist(), 0.0], [*hi.tolist(), float(layers[-1].z)]],
                     vase_min=round(vase_min, 1), normal_min=round(stats["print_time_min"], 1), wall_mm=round(wall, 2),
                     line_mm=round(lw, 2))]


def _join(pairs: np.ndarray, n: int) -> np.ndarray:
    """Connected groups of ``n`` items linked by ``pairs``: the smallest index in each group, per item
    (plain numpy, so the check needs no scipy in the browser)."""
    label = np.arange(n)
    if not len(pairs):
        return label
    a, b = pairs[:, 0], pairs[:, 1]
    while True:
        low = np.minimum(label[a], label[b])
        new = label.copy()
        np.minimum.at(new, a, low)
        np.minimum.at(new, b, low)
        new = new[new]  # jump to the group's lowest label
        if np.array_equal(new, label):
            return label
        label = new


def coarse_surfaces(mesh: Mesh, T: dict | None = None) -> list[dict]:
    """Curved surfaces exported with too few flat facets (``facets`` in thresholds.yaml): rows of flat
    strips that each turn a little from the next. Each: {"bbox", "strips", "turn_deg" (per strip, so about
    360 / turn strips per full circle), "width_mm", "error_mm" (how far a strip's middle sits inside the
    true curve), "area_mm2"}."""
    from .regions import face_adjacency, weld

    F = (T or load_data("thresholds"))["facets"]
    normals, areas = mesh.normals_and_areas()
    adj = face_adjacency(weld(mesh))
    if not len(adj):
        return []
    cos = np.einsum("ij,ij->i", normals[adj[:, 0]], normals[adj[:, 1]])
    patch = _join(adj[cos > np.cos(np.radians(0.5))], mesh.face_count)  # flat strips
    pa, pb = patch[adj[:, 0]], patch[adj[:, 1]]
    keep = pa != pb
    pairs = np.unique(np.sort(np.stack([pa[keep], pb[keep]], axis=1), axis=1), axis=0)
    if not len(pairs):
        return []
    ids, inv = np.unique(patch, return_inverse=True)  # patches as 0..k-1
    k = len(ids)
    pairs = np.searchsorted(ids, pairs)
    n = np.stack([np.bincount(inv, normals[:, d] * areas, minlength=k) for d in range(3)], axis=1)
    n /= np.maximum(np.linalg.norm(n, axis=1), 1e-12)[:, None]
    area = np.bincount(inv, areas, minlength=k)
    t = mesh.triangles
    longest = np.zeros(k)
    np.maximum.at(longest, inv, np.linalg.norm(t - np.roll(t, 1, axis=1), axis=2).max(axis=1))
    width = area / np.maximum(longest, 1e-12)  # a strip's width across the curve
    turn = np.degrees(np.arccos(np.clip(np.einsum("ij,ij->i", n[pairs[:, 0]], n[pairs[:, 1]]), -1, 1)))
    arc = pairs[(turn >= F["min_turn_deg"]) & (turn <= F["max_turn_deg"])]
    turn = turn[(turn >= F["min_turn_deg"]) & (turn <= F["max_turn_deg"])]
    links = np.bincount(arc.ravel(), minlength=k)
    inner = links >= 2  # strips with a curve on both sides
    use = inner[arc[:, 0]] | inner[arc[:, 1]]
    arc, turn = arc[use], turn[use]
    if not len(arc):
        return []
    w = np.minimum(width[arc[:, 0]], width[arc[:, 1]])
    error = w / 2 * np.tan(np.radians(turn) / 4)
    group = _join(arc, k)
    in_curve = np.zeros(k, bool)
    in_curve[arc.ravel()] = True
    out = []
    centres = t.mean(axis=1)
    for g in np.unique(group[arc[:, 0]]):
        strips = np.flatnonzero(in_curve & (group == g))
        rows = group[arc[:, 0]] == g
        err = float(np.median(error[rows]))
        if len(strips) < F["min_strips"] or err < F["error_mm"]:
            continue
        tri = np.isin(inv, strips)
        pts = t[tri].reshape(-1, 3)
        out.append({"bbox": [pts.min(axis=0).round(2).tolist(), pts.max(axis=0).round(2).tolist()], "strips": int(len(strips)),
                    "turn_deg": round(float(np.median(turn[rows])), 1), "width_mm": round(float(np.median(w[rows])), 2),
                    "error_mm": round(err, 3), "area_mm2": round(float(area[strips].sum())),
                    "centre": centres[tri].mean(axis=0).round(2).tolist()})
    return sorted(out, key=lambda c: -c["error_mm"])


def facet_findings(mesh: Mesh, T: dict) -> list[dict]:
    """Curves made of too few facets print as flats and make holes / fits the wrong size (coarse_facets)."""
    F = T["facets"]
    out = []
    for c in coarse_surfaces(mesh, T):
        sides = 360 / max(c["turn_deg"], 1e-6)
        text = (f"Curved surface made of flat strips about {c['width_mm']:.1f} mm wide (about {sides:.0f} per full circle): it prints "
                f"with flats, up to {c['error_mm']:.2f} mm off the round shape, and a hole or peg there comes out the wrong size. "
                "Export a finer STL (smaller deviation / more facets) or use the STEP file")
        out.append(_finding("coarse_facets", "warn" if c["error_mm"] >= F["warn_mm"] else "info", text, bbox=c["bbox"],
                            **{k: v for k, v in c.items() if k != "bbox"}))
    return out


def fine_layer_ranges(mesh: Mesh, profile: Profile, T: dict | None = None) -> list[dict]:
    """Height ranges of shallow top slopes (``slopes`` in thresholds.yaml) in the oriented ``mesh`` whose
    layer steps show at the user's layer height: {"z0", "z1", "slope_deg", "area_mm2", "layer_height" (the
    thinner one), "step_mm" now, "fine_step_mm" with it}. Empty when thinner layers wouldn't help."""
    S = (T or load_data("thresholds"))["slopes"]
    lh = goal_plan(profile)["layer_height"]
    fine = round(max(S["fine_min_mm"], S["fine_ratio"] * float(profile.nozzle)) / 0.02) * 0.02
    if fine > S["fine_gain"] * lh:  # not much thinner than now: the steps would hardly change
        return []
    normals, areas = mesh.normals_and_areas()
    nz = normals[:, 2]
    shallow = (nz > np.cos(np.radians(S["max_deg"]))) & (nz < np.cos(np.radians(S["min_deg"]))) & (areas > 0)
    if not shallow.any():
        return []
    z = mesh.triangles[shallow][..., 2]
    lo, hi, a, n = z.min(axis=1), z.max(axis=1), areas[shallow], nz[shallow]
    order = np.argsort(lo)
    ranges, cur = [], None
    for i in order:  # faces overlapping in height (or nearly) make one range
        if cur is not None and lo[i] <= cur[1] + S["merge_gap_mm"]:
            cur[1] = max(cur[1], hi[i])
            cur[2].append(i)
        else:
            cur = [lo[i], hi[i], [i]]
            ranges.append(cur)
    out = []
    for z0, z1, idx in ranges:
        area = float(a[idx].sum())
        slope = float(np.degrees(np.arccos(np.clip(np.average(n[idx], weights=a[idx]), -1, 1))))
        step = lh / np.tan(np.radians(slope))
        if area < S["min_area_mm2"] or step < S["visible_step_mm"]:
            continue
        out.append({"z0": round(float(z0), 2), "z1": round(float(z1), 2), "slope_deg": round(slope, 1), "area_mm2": round(area),
                    "layer_height": round(fine, 2), "step_mm": round(float(step), 2),
                    "fine_step_mm": round(float(fine / np.tan(np.radians(slope))), 2)})
    return out


def slope_findings(mesh: Mesh, profile: Profile, T: dict) -> list[dict]:
    """Shallow top slopes that print as stairs, with thinner layers just there (shallow_slope_stairs)."""
    lh = goal_plan(profile)["layer_height"]
    out = []
    for r in fine_layer_ranges(mesh, profile, T):
        lo, hi = mesh.bounds()
        text = (f"Shallow slope (about {r['slope_deg']:.0f}° from flat) at {r['z0']:.1f}–{r['z1']:.1f} mm: at {lh:.2f} mm layers it "
                f"prints as steps about {r['step_mm']:.1f} mm wide. Thinner {r['layer_height']:.2f} mm layers just there make them "
                f"{r['fine_step_mm']:.1f} mm; the 3MF can carry that height range")
        out.append(_finding("shallow_slope_stairs", "info", text, bbox=[[float(lo[0]), float(lo[1]), r["z0"]], [float(hi[0]), float(hi[1]), r["z1"]]], **r))
    return out


def _fastener(d: float, T: dict) -> dict:
    """{"fastener": "M3"} when the hole is modelled at a metric screw's nominal size."""
    F = T["fasteners"]
    for name, f in F.items():
        if isinstance(f, dict) and abs(d - f["d"]) <= F["match_tol_mm"]:
            return {"fastener": name}
    return {}


def fastener_advice(d: float, T: dict) -> str:
    """Hole sizes for the usual ways of fastening, when the hole is exactly screw-sized."""
    name = _fastener(d, T).get("fastener")
    if not name:
        return ""
    f = T["fasteners"][name]
    return (f". Looks like a hole for an {name} screw: for the screw to pass through, model it Ø{f['clearance_normal_mm']} mm; "
            f"to screw into the plastic, Ø{f['pilot_mm']} mm; for a heat-set insert, Ø{f['insert_hole_mm']} mm")


def _sideways_hole_findings(mesh: Mesh, profile: Profile, T: dict, findings: list[dict]) -> list[dict]:
    """Holes lying sideways: the bottom half prints like a vertical hole, the top is a bridge that sags.
    A sag finding for the hole replaces the generic bridge-sag finding at the same spot."""
    H, B = T["holes"], T["bridges"]
    nozzle = float(profile.nozzle)
    min_d = max(H["vertical_min_mm"] * nozzle / 0.4, H["vertical_min_nozzle"] * nozzle)
    out = []
    for h in horizontal_holes(mesh):
        d, (cx, cy, cz) = h["diameter"], h["center"]
        where = f"at {cz:.1f} mm height"
        metrics = {"diameter": round(d, 2), "axis": h["axis"], "center": h["center"]}
        if d < min_d:
            out.append(_finding("hole_closes", "problem",
                                f"Sideways Ø{d:.1f} mm hole {where} — too small for a {profile.nozzle} mm nozzle (min ~{min_d:.1f} mm), it will close up",
                                bbox=h["bbox"], **metrics))
        elif d < B["sag_from_mm"]:
            lo, hi = H["typical_undersize_mm"]
            out.append(_finding("small_hole_undersized", "warn",
                                f"Sideways Ø{d:.1f} mm hole {where} prints about {lo}–{hi} mm too small — a screw or pin of that size won't fit without drilling"
                                + fastener_advice(d, T), bbox=h["bbox"], **metrics, **_fastener(d, T)))
        else:
            lo, hi = B["sag_mm"] if d < B["large_sag_from_mm"] else (0.4, 1.0)
            out.append(_finding("sideways_hole_oval", "warn",
                                f"Sideways Ø{d:.1f} mm hole {where}: its top sags {lo}–{hi} mm, so it prints oval "
                                f"(about Ø{d - hi:.1f}–{d - lo:.1f} mm top to bottom) — a Ø{d:.0f} pin or bolt won't slide in"
                                + fastener_advice(d, T), bbox=h["bbox"], sag_mm=[lo, hi], **metrics, **_fastener(d, T)))
        # Too wide to bridge: the slicer supports the roof. In a blind or long narrow tunnel that
        # support can't be reached to pull it out.
        if d > profile.material["max_bridge_mm"] and (not h["through"] and h["length"] > d or h["length"] > B["tunnel_ratio"] * d):
            kind = "blind" if not h["through"] else "long"
            out.append(_finding("trapped_support", "warn",
                                f"Sideways Ø{d:.0f} mm hole {where} ({kind}, ~{h['length']:.0f} mm deep) needs support inside, "
                                f"and it can't be reached to pull it out", bbox=h["bbox"], **metrics))
    holes = [f["bbox"] for f in out if f["rule"] == "sideways_hole_oval"]
    kept = [f for f in findings if not (f["rule"] == "bridge_sag" and f["bbox"] and any(_inside(f["bbox"], b) for b in holes))]
    return kept + out


def _inside(box, outer, tol: float = 1.0) -> bool:
    """Box centre inside ``outer`` (both [[x0,y0,z0],[x1,y1,z1]]), with some slack."""
    c = (np.asarray(box[0]) + np.asarray(box[1])) / 2
    return bool(np.all(c >= np.asarray(outer[0]) - tol) and np.all(c <= np.asarray(outer[1]) + tol))


def _side_relief_findings(mesh: Mesh, profile: Profile, T: dict) -> list[dict]:
    """Text and details on faces that stand upright when printed: small flat patches a little in front of
    (raised) or behind (engraved) a bigger face with the same direction. Reported when the letters are
    shorter than the layers can draw, or engraving is deep enough to droop."""
    R = T["relief"]
    normals, areas = mesh.normals_and_areas()
    side = (np.abs(normals[:, 2]) < 0.02) & (areas > 1e-9)
    if not side.any() or side.sum() > 200_000:
        return []
    n = normals[side]
    ang = np.round(np.degrees(np.arctan2(n[:, 1], n[:, 0]))).astype(int) % 360
    tris = mesh.triangles[side]
    out = []
    for a in np.unique(ang):
        sel = ang == a
        d = np.array([np.cos(np.radians(a)), np.sin(np.radians(a)), 0.0])
        u = np.array([-d[1], d[0], 0.0])
        offset = np.round(tris[sel].mean(axis=1) @ d, 2)
        flat = np.stack([tris[sel] @ u, tris[sel][..., 2]], axis=-1)  # (along the face, height)
        planes = {}
        for o in np.unique(offset):
            polys = shapely.polygons(flat[offset == o])
            merged = shapely.union_all(shapely.make_valid(polys[shapely.area(polys) > 1e-9]), grid_size=1e-4)
            planes[float(o)] = [p for p in parts(merged, 0.0) if p.area > 0.01]
        offs = np.array(sorted(planes))
        for o1, pieces in planes.items():
            near = offs[(np.abs(offs - o1) > 1e-3) & (np.abs(offs - o1) <= R["side_max_offset_mm"])]
            for o0 in near.tolist():
                depth, raised = abs(o1 - o0), o1 > o0
                found = []
                for base in planes[o0]:
                    shell = Polygon(base.exterior)
                    found += [p for p in pieces if p.area <= R["max_share"] * shell.area and shell.contains(p.buffer(-0.01))]
                if not found:
                    continue
                heights = [p.bounds[3] - p.bounds[1] for p in found]
                tall = float(np.median(heights))
                small = tall < R["side_min_height_mm"]
                droop = not raised and depth > R["side_max_depth_mm"]
                if not (small or droop):
                    continue
                why = []
                if small:
                    why.append(f"only about {tall:.1f} mm tall ({tall / profile.layer_height:.0f} layers) — make it at least {R['side_min_height_mm']:.0f} mm")
                if droop:
                    why.append(f"engraved {depth:.1f} mm deep — the top of each letter droops past {R['side_max_depth_mm']} mm")
                pts = np.concatenate([np.asarray(p.exterior.coords) for p in found])
                corners = [o1 * d + pts[:, 0].min() * u + [0, 0, pts[:, 1].min()], o1 * d + pts[:, 0].max() * u + [0, 0, pts[:, 1].max()]]
                lo, hi = np.minimum(*corners), np.maximum(*corners)
                out.append(_finding("side_detail_small", "info",
                                    f"{'Raised' if raised else 'Engraved'} detail on an upright face ({len(found)} piece(s)): " + "; ".join(why),
                                    bbox=[lo.tolist(), hi.tolist()], height_mm=round(tall, 2), depth_mm=round(depth, 2), pieces=len(found)))
    return out


def _use_heat_findings(profile: Profile, T: dict) -> list[dict]:
    """The part gets near or past the temperature where the material goes soft."""
    from .service import heat_factor, heat_limit

    t = profile.use_temp_c
    if t is None:
        return []
    limit = heat_limit(profile)
    if t < limit - T["use_heat"]["warn_within_c"]:
        return []
    name, k = profile.material["name"], heat_factor(profile)
    if t >= limit:
        return [_finding("too_hot_in_use", "problem", f"At {t:.0f} °C {name} is soft (it goes soft around {limit:.0f} °C): "
                         f"only about {k:.0%} of its stiffness is left, it will bend or sag", use_temp_c=t, heat_limit_c=limit)]
    return [_finding("too_hot_in_use", "warn", f"At {t:.0f} °C {name} is close to where it goes soft (~{limit:.0f} °C): "
                     f"about {k:.0%} of its strength is left, and loads that stay on make it creep", use_temp_c=t, heat_limit_c=limit)]


def flat_gaps(mesh: Mesh, T: dict | None = None) -> list[dict]:
    """Flat gaps stacked in height (``gaps`` in thresholds.yaml): a down-facing flat face with an up-facing one
    under it, closer than ``z_fuse_below_mm`` and overlapping by ``z_min_area_mm2`` or more. Each: {"bbox",
    "gap_mm", "area_mm2", "z"} (the ceiling's height)."""
    G = (T or load_data("thresholds"))["gaps"]
    normals, areas = mesh.normals_and_areas()
    z = mesh.triangles[..., 2].mean(axis=1)
    lowest = mesh.triangles[..., 2].min()
    flat = areas > 1e-9
    up, down = flat & (normals[:, 2] > 0.99), flat & (normals[:, 2] < -0.99) & (z > lowest + 0.05)
    if not up.any() or not down.any() or up.sum() + down.sum() > 200_000:
        return []

    def levels(mask):
        # Snap-rounded union of valid triangles, as _relief_findings: the browser's older GEOS throws on (and
        # can't recover from) a plain union of touching triangles.
        zz, tris = np.round(z[mask], 2), mesh.triangles[mask][..., :2]
        out = {}
        for level in np.unique(zz):
            polys = shapely.polygons(tris[zz == level])
            out[float(level)] = shapely.union_all(shapely.make_valid(polys[shapely.area(polys) > 1e-9]), grid_size=1e-4)
        return out

    floors, ceilings = levels(up), levels(down)
    out = []
    for zc, ceiling in ceilings.items():
        for zf, floor in floors.items():
            gap = zc - zf
            if not 0.005 < gap < G["z_fuse_below_mm"]:
                continue
            both = _safe_overlay(shapely.intersection, ceiling, floor)
            if both.area < G["z_min_area_mm2"]:
                continue
            x0, y0, x1, y1 = both.bounds
            out.append({"bbox": [[x0, y0, zf], [x1, y1, zc]], "gap_mm": round(gap, 2), "area_mm2": round(both.area, 1), "z": zc})
    return out


def flat_gap_findings(mesh: Mesh, profile: Profile, T: dict) -> list[dict]:
    """Print-in-place joints with a flat gap in height: bridged over the part below, it fuses (flat_gap_fuses)."""
    out = []
    for g in flat_gaps(mesh, T):
        text = (f"Flat gap of {g['gap_mm']:.2f} mm in height at {g['z']:.1f} mm ({g['area_mm2']:.0f} mm²): the face above it is "
                "printed in mid-air right over the plastic below, sags, and welds the two together. Use 45° cones with a "
                "0.2–0.3 mm gap instead, or at least 0.4–0.6 mm for a short bridge")
        if g["gap_mm"] < profile.layer_height:
            text += f" — it is thinner than a layer ({profile.layer_height:.2f} mm), so it isn't even printed as a gap"
        out.append(_finding("flat_gap_fuses", "problem", text, bbox=g["bbox"], gap_mm=g["gap_mm"], area_mm2=g["area_mm2"]))
    return out


def _relief_findings(mesh: Mesh, profile: Profile, T: dict) -> list[dict]:
    """Text and details raised from / sunk into an up-facing face by only a layer or two: a small flat
    patch a little above (raised) or below (engraved) a bigger flat face that surrounds it."""
    R = T["relief"]
    lh = profile.layer_height
    limit = max(R["min_mm"], R["min_layers"] * lh)
    normals, areas = mesh.normals_and_areas()
    up = (normals[:, 2] > 0.99) & (areas > 1e-9)
    if not up.any() or up.sum() > 200_000:
        return []
    z = np.round(mesh.triangles[up][..., 2].mean(axis=1), 2)
    tris = mesh.triangles[up][..., :2]
    levels = {}
    for level in np.unique(z):
        polys = shapely.polygons(tris[z == level])
        merged = shapely.union_all(shapely.make_valid(polys[shapely.area(polys) > 1e-9]), grid_size=1e-4)
        levels[float(level)] = [p for p in parts(merged, 0.0) if p.area > 0.01]
    found: dict[tuple, list] = {}
    zs = np.array(sorted(levels))
    for z1, pieces in levels.items():
        near = zs[(np.abs(zs - z1) > 1e-3) & (np.abs(zs - z1) < limit - 1e-6)]  # only levels close in height
        for z0 in near.tolist():
            d, bases = z1 - z0, levels[z0]
            for base in bases:
                shell = Polygon(base.exterior)
                for piece in pieces:
                    if piece.area <= R["max_share"] * shell.area and shell.contains(piece.buffer(-0.01)):
                        found.setdefault((round(abs(d), 2), d > 0, z0), []).append(piece)
    out = []
    for (d, raised, z0), pieces in found.items():
        layers = d / lh
        sev = "warn" if layers < R["vanish_layers"] else "info"
        what = "Raised" if raised else "Engraved"
        x0, y0, x1, y1 = shapely.union_all(pieces).bounds
        top = z0 + (d if raised else 0)
        out.append(_finding("shallow_relief", sev,
                            f"{what} detail only {d:.2f} mm {'high' if raised else 'deep'} ({layers:.1f} layers of {lh} mm) on the face at "
                            f"{z0:.1f} mm — {'may not show at all' if sev == 'warn' else 'reads faintly'}; make it {limit:.1f} mm or more",
                            bbox=[[x0, y0, top - d], [x1, y1, top]], depth_mm=d, layers=round(layers, 1), pieces=len(pieces)))
    return out


def _shrinkage_findings(mesh: Mesh, profile: Profile, T: dict) -> list[dict]:
    """Long parts come out measurably short: plastic shrinks as it cools."""
    pct = profile.material.get("shrinkage_pct")
    if not pct:
        return []
    S = T["shrinkage"]
    lo, hi = mesh.bounds()
    length = float(max(hi - lo))
    short = length * pct / 100
    if short < S["report_from_mm"]:
        return []
    sev = "warn" if short >= S["warn_from_mm"] else "info"
    return [_finding("shrinkage_size_error", sev,
                     f"{profile.material['name']} shrinks about {pct}% as it cools: the {length:.0f} mm length comes out "
                     f"roughly {short:.1f} mm short — matters if it has to fit something", bbox=[lo.tolist(), hi.tolist()],
                     shrinkage_pct=pct, length_mm=round(length, 1), short_mm=round(short, 2))]


def _first_layer_findings(layers: list[Layer], profile: Profile, T: dict) -> list[dict]:
    """Fine detail on the bed side (text, logos, small gaps) gets squashed by the first layers."""
    FL = T["first_layer"]
    r = FL["detail_min_mm"] / 2
    pieces = []
    for layer in layers:
        if layer.z > FL["zone_mm"]:
            break
        g = layer.geom
        if g.is_empty:
            continue
        pieces += parts(_safe_diff(g, _opening(g, r)), 0.3)  # thin strokes
        pieces += parts(_safe_diff(_closing(g, r), g), 0.3)  # narrow engraved lines / gaps
    if not pieces:
        return []
    u = shapely.union_all(pieces)
    return [_finding("first_layer_detail", "warn",
                     f"Fine detail (under {FL['detail_min_mm']} mm) on the bed side, ~{u.area:.0f} mm² — text or edges here get squashed",
                     bbox=_layer_bbox(u, 0.0, FL["zone_mm"]))]


# ---------------------------------------------------------------- bed contact


def _bed_findings(layers: list[Layer], profile: Profile, T: dict) -> list[dict]:
    out = []
    first = layers[0].geom
    mat = profile.material
    lh = profile.layer_height
    if first.is_empty:
        return [_finding("small_bed_contact", "problem", "Nothing touches the bed in this orientation")]
    height = layers[-1].z
    max_area = max(l.geom.area for l in layers)
    bed_bbox = _layer_bbox(first, 0.0, lh)
    S = T["stability"]

    if first.area < 0.15 * max_area or (first.area < S["min_bed_contact_mm2"] and height > 20):
        out.append(_finding("small_bed_contact", "warn" if first.area < 40 else "info",
                            f"Only {first.area:.0f} mm² touches the bed (largest layer {max_area:.0f} mm²)", bbox=bed_bbox, bed_area=first.area))
    width = _min_width(first.convex_hull)
    if width > 0 and height / width > S["tip_aspect"]:
        out.append(_finding("tipping_risk", "warn", f"{height:.0f} mm tall on a base only {width:.0f} mm wide", bbox=bed_bbox))

    # Warping: long footprints in shrinking materials lift at their corners.
    W = T["warping"]
    measured = profile.material.get("warp_length_mm")  # from the user's warping calibration print
    limit = (measured or W["risk_length_mm"].get(profile.material.get("based_on", profile.material_id), {})).get(profile.enclosure)
    if limit:
        for island in parts(first, 1.0):
            length = _rect_sides(island)[1]
            ratio = length / limit
            corners = _sharp_corners(island, W["sharp_corner_max_deg"])
            if ratio < 0.7 and not (corners and ratio >= 0.4 and mat["warp_risk"] != "low"):
                continue
            sev = "problem" if ratio >= 1.3 else "warn" if ratio >= 0.7 else "info"
            where = f", worst at {len(corners)} sharp corner(s)" if corners else ""
            out.append(_finding("warping", sev,
                                f"{length:.0f} mm long base in {mat['name']} ({profile.printer['name']}) — corners likely lift{where}",
                                bbox=_layer_bbox(island, 0.0, lh), points=corners, length_mm=length, risk_length_mm=limit))
    if any(len(p.interiors) for p in parts(first)):
        lo, hi = T["first_layer"]["elephant_foot_mm"]
        out.append(_finding("elephant_foot", "info",
                            f"Holes and bottom edges at the bed bulge {lo}–{hi} mm (elephant foot) — holes there come out tight", bbox=bed_bbox))
    return out


def _sharp_corners(geom, max_angle=100.0, min_edge=2.0) -> list[list[float]]:
    """Convex outline corners sharper than ``max_angle`` degrees with both edges ≥ ``min_edge`` mm."""
    found = []
    for poly in parts(geom):
        # Simplify first: mesh edges leave collinear points that split long outline edges.
        ring = shapely.geometry.polygon.orient(poly.simplify(0.05)).exterior  # CCW
        c = np.asarray(ring.coords)[:-1]
        prev, nxt = np.roll(c, 1, axis=0), np.roll(c, -1, axis=0)
        e1, e2 = c - prev, nxt - c
        l1, l2 = np.linalg.norm(e1, axis=1), np.linalg.norm(e2, axis=1)
        cross = e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]
        cos_turn = np.einsum("ij,ij->i", e1, e2) / np.maximum(l1 * l2, 1e-12)
        interior = 180.0 - np.degrees(np.arccos(np.clip(cos_turn, -1, 1)))
        mask = (cross > 0) & (interior < max_angle) & (l1 >= min_edge) & (l2 >= min_edge)
        found += c[mask].round(2).tolist()
    return found


# ---------------------------------------------------------------- weakness


def _weak_findings(layers: list[Layer], tracker: Tracker, profile: Profile, T: dict) -> list[dict]:
    out = []
    P, N = T["pins"], T["necks"]
    for r in tracker.of("slender"):
        h, w = r.z1 - r.z0, r.metrics["width"]
        if h >= P["wobble_aspect"] * w and h > 10:
            out.append(_finding("tall_thin_feature", "warn", f"Slender feature ~{w:.1f} mm wide and {h:.0f} mm tall wobbles while printing", r, profile))
        if h >= P["min_aspect"] * w and r.metrics["length"] < 2 * P["max_width_mm"] and _free_top(r, layers):
            out.append(_finding("weak_upright_pin", "warn",
                                f"Pin/post ~{w:.1f} mm wide stands {h:.0f} mm upright — the layers run across it, so it snaps easily", r, profile))
    # Necks: a layer much smaller than the part above and below it.
    areas = np.array([l.geom.area for l in layers])
    zs = np.array([l.z for l in layers])
    neck = []
    for i in range(1, len(layers) - 1):
        below = areas[(zs < zs[i]) & (zs >= zs[i] - N["window_mm"])]
        above = areas[(zs > zs[i]) & (zs <= zs[i] + N["window_mm"])]
        if len(below) and len(above) and 0 < areas[i] < N["area_ratio"] * min(below.max(), above.max()):
            neck.append(i)
    for run in _runs(neck):
        i = min(run, key=lambda k: areas[k])
        g = layers[i].geom
        out.append(_finding("thin_neck", "warn",
                            f"Narrow neck at {zs[i]:.1f} mm ({areas[i]:.0f} mm² between bigger sections) — everything above hangs on a few layers",
                            bbox=_layer_bbox(g, layers[run[0]].z, layers[run[-1]].z)))
    return out


def _free_top(r: Region, layers: list[Layer]) -> bool:
    """A pin sticks out: nothing continues above its top. (Ribs between slots join material above.)"""
    if r.last_index + 1 >= len(layers):
        return True
    above = layers[r.last_index + 1].geom.intersection(r.last.buffer(0.2, quad_segs=QUAD))
    return above.area < 0.2 * r.last.area


# ---------------------------------------------------------------- parts that bend on purpose, thick blocks (T-181)


def _flex_findings(layers: list[Layer], profile: Profile, T: dict) -> list[dict]:
    """Living hinges, snap-fit tabs standing upright, and solid blocks better made as ribbed shells."""
    X = T["flex"]
    mat = profile.material.get("based_on", profile.material_id)
    out = _flat_hinges(layers, profile, X, mat) + _upright_hinges(layers, profile, X, mat)
    out += _upright_snaps(layers, profile, X, mat) + _thick_blocks(layers, profile, X)
    return out


def _hinge_note(mat: str, X: dict, name: str) -> str:
    if mat in X["hinge_good"]:
        return f"{name} bends well"
    if mat in X["hinge_bad"]:
        return f"{name} cracks after a couple of hundred bends: print it in PETG or TPU if it has to bend often"
    return f"{name} bends a while; PETG or TPU last longer"


def _flat_hinges(layers: list[Layer], profile: Profile, X: dict, mat: str) -> list[dict]:
    """A thin bottom band that is the only link between two pieces standing on it: a hinge printed flat.
    Seen where one piece of a layer splits into two or more on the next layer."""
    out, lo, hi = [], *X["hinge_gap_mm"]
    for i in range(1, len(layers)):
        below, above = layers[i - 1].geom, layers[i].geom
        for band in parts(below, 1.0):
            pieces = [p for p in parts(above, 2.0) if p.intersects(band)]
            if len(pieces) < 2:
                continue
            a, b = sorted(pieces, key=lambda p: -p.area)[:2]
            gap = a.distance(b)
            if not lo <= gap <= hi:
                continue
            web = _safe_diff(band, shapely.union_all([a, b]).buffer(0.05, quad_segs=QUAD))
            web = web.intersection(a.buffer(gap + 0.1, quad_segs=QUAD)).intersection(b.buffer(gap + 0.1, quad_segs=QUAD))
            long = web.area / max(gap, 1e-6)
            if web.is_empty or long < X["hinge_min_long_mm"]:
                continue
            spot = web.representative_point()
            k = i - 1
            while k > 0 and layers[k - 1].geom.contains(spot):
                k -= 1
            t = layers[i - 1].z - layers[k].z + profile.layer_height
            if t > X["hinge_max_mm"] or _band_ends(layers, i, a, b, t):
                continue
            x0, y0, x1, y1 = web.bounds
            sev = "warn" if mat in X["hinge_bad"] else "info"
            out.append(_finding("living_hinge", sev,
                                f"Thin hinge {t:.1f} mm thick and {long:.0f} mm long between two parts {gap:.1f} mm apart, printed flat "
                                f"(the right way). {_hinge_note(mat, X, profile.material['name'])}",
                                bbox=[[x0, y0, layers[k].z - profile.layer_height / 2], [x1, y1, layers[i - 1].z + profile.layer_height / 2]],
                                thickness_mm=round(t, 2), length_mm=round(long, 1), gap_mm=round(gap, 2)))
    return out


def _band_ends(layers, i: int, a: Polygon, b: Polygon, t: float) -> bool:
    """The two pieces don't stand up past the band by twice its thickness: just steps in a thin plate."""
    top = layers[i].z + 2 * t
    for j in range(i, len(layers)):
        if layers[j].z >= top:
            g = layers[j].geom
            return not (g.intersects(a.representative_point().buffer(0.3)) and g.intersects(b.representative_point().buffer(0.3)))
    return True


def _upright_hinges(layers: list[Layer], profile: Profile, X: dict, mat: str) -> list[dict]:
    """A few layers where the whole cross-section is a thin strip, between thick sections: a hinge (or
    thin link) printed standing up, so it bends across its layers."""
    t, out = X["hinge_max_mm"], []
    thin = [i for i in range(1, len(layers) - 1)
            if not layers[i].geom.is_empty and layers[i].geom.area > 0.5 and _opening(layers[i].geom, t / 2 + 0.05).is_empty]
    for run in _runs(thin):
        z0, z1 = layers[run[0]].z, layers[run[-1]].z
        if z1 - z0 > 6.0 or run[0] == 0 or run[-1] + 1 >= len(layers):
            continue
        neighbours = [layers[run[0] - 1].geom, layers[run[-1] + 1].geom]
        if any(_opening(g, t).area < 5 * layers[run[0]].geom.area for g in neighbours):
            continue
        g = layers[run[0]].geom
        out.append(_finding("living_hinge", "problem",
                            f"Thin strip {_rect_sides(g.convex_hull)[0]:.1f} mm thick at {z0:.1f} mm joins the parts above and below: if it is "
                            f"meant to bend (a hinge), it bends across its layers and breaks. Lay the part so the strip lies flat. "
                            f"{_hinge_note(mat, X, profile.material['name'])}",
                            bbox=_layer_bbox(g, z0 - profile.layer_height / 2, z1 + profile.layer_height / 2), height_mm=round(z1 - z0, 2)))
    return out


def _upright_snaps(layers: list[Layer], profile: Profile, X: dict, mat: str) -> list[dict]:
    """Thin tabs that rise from the part, end free and get thicker at the tip (a hook): snap fits that
    bend across their layers. Says how deep a hook the tab takes, flat and as printed."""
    lo, hi = X["snap_mm"]
    chains, done = [], []
    for i, layer in enumerate(layers):
        live = []
        for p in parts(layer.geom, 0.3):
            w, _ = _rect_sides(p)
            if w > hi + 3.0 or p.area > 300:
                continue
            c = next((c for c in chains if c["piece"].intersects(p)), None)
            if c is None:
                c = {"start": i, "ws": [], "piece": p}
            c["ws"].append(w)
            c["piece"], c["end"] = p, i
            live.append(c)
        done += [c for c in chains if c not in live]
        chains = live
    done += chains
    out = []
    for c in done:
        s, e, ws = c["start"], c["end"], np.array(c["ws"])
        if s == 0 or len(ws) < 6:
            continue
        base = layers[s - 1].geom
        if base.intersection(c["piece"].buffer(0.5)).is_empty or base.area < 3 * c["piece"].area:
            continue  # not standing on the part
        if e + 1 < len(layers) and layers[e + 1].geom.intersects(c["piece"]):
            continue  # not free at the top
        cut = max(1, int(0.7 * len(ws)))
        stem, hook = float(np.median(ws[:cut])), float(ws[cut:].max() - np.median(ws[:cut]))
        length = layers[e].z - layers[s].z
        if not lo <= stem <= hi or length < X["snap_min_aspect"] * stem or hook < X["hook_min_mm"]:
            continue
        eps = X["snap_strain_pct"].get(mat, 1.5) / 100
        flat = 0.67 * eps * length ** 2 / stem
        upright = flat * X["across_layers"]
        sev = "problem" if hook > upright else "warn"
        x0, y0, x1, y1 = c["piece"].bounds
        bx = layers[s].geom.intersection(c["piece"].buffer(stem + 3.0)).bounds or (x0, y0, x1, y1)
        out.append(_finding("snap_fit_upright", sev,
                            f"Snap-fit tab {length:.0f} mm tall, {stem:.1f} mm thick, hook {hook:.1f} mm, standing upright: it bends across its "
                            f"layers and takes a hook of only about {upright:.1f} mm in {profile.material['name']}. Lying flat it would take about {flat:.1f} mm",
                            bbox=[[min(x0, bx[0]), min(y0, bx[1]), layers[s].z], [max(x1, bx[2]), max(y1, bx[3]), layers[e].z]],
                            length_mm=round(length, 1), thickness_mm=round(stem, 2), hook_mm=round(hook, 2),
                            hook_max_mm=round(upright, 2), hook_max_flat_mm=round(flat, 2)))
    return out


def _thick_blocks(layers: list[Layer], profile: Profile, X: dict) -> list[dict]:
    """How much of the part is thicker than ``thick_mm`` all round (morphological opening per layer)."""
    r = X["thick_mm"] / 2
    thick = total = 0.0
    box = None
    cached = None
    for layer in layers:
        g = layer.geom
        total += g.area * layer.dz
        if g.is_empty:
            continue
        x0, y0, x1, y1 = g.bounds
        if min(x1 - x0, y1 - y0) < 2 * r:
            continue
        if cached is not None and g.equals_exact(cached[0], 1e-4):
            area, b = cached[1]
        else:
            area, b = _thick_core(g, r)
        cached = (g, (area, b))
        if b is None:
            continue
        thick += area * layer.dz
        box = b + (layer.z, layer.z) if box is None else (min(box[0], b[0]), min(box[1], b[1]), max(box[2], b[2]), max(box[3], b[3]), box[4], layer.z)
    if box is None or thick / 1000 < X["thick_min_cm3"] or thick < X["thick_min_share"] * total:
        return []
    return [_finding("thick_solid_block", "info",
                     f"About {thick / 1000:.0f} cm³ of the part ({thick / total:.0%}) is a solid block more than {X['thick_mm']:.0f} mm thick. "
                     f"The slicer fills it with infill: walls give it its strength, and a ribbed shell is stiffer per gram",
                     bbox=[[box[0], box[1], box[4]], [box[2], box[3], box[5]]], thick_cm3=round(thick / 1000, 1), share=round(thick / total, 2))]


def _runs(idx: list[int]) -> list[list[int]]:
    runs: list[list[int]] = []
    for i in idx:
        if runs and i == runs[-1][-1] + 1:
            runs[-1].append(i)
        else:
            runs.append([i])
    return runs


# ---------------------------------------------------------------- time, cooling, setup


# Print-time model, fitted to OrcaSlicer's own estimates for the samples (P1S and K1C system
# profiles, generic PLA, balanced and speed goals; tools/time_fit.py): typical ×1.11, worst ×1.16 of
# the two printers' mean, which differ ×1.11 from each other. Real moves rarely reach nominal speed.
TIME_EFFICIENCY = 0.6
LAYER_OVERHEAD_S = 7.0  # layer change, travel, retractions, slow first moves
START_MIN = 3.0  # heating, homing, bed levelling, purge line


def print_minutes(volume_mm3: float, profile: Profile) -> float:
    """Rough minutes to print ``volume_mm3`` of infill and walls inside the part: the infill speed of
    ``_estimate`` (or the melt-rate limit). For what more or less plastic costs, not a whole print."""
    lw, lh = profile.line_width, goal_plan(profile)["layer_height"]
    v = min(profile.speeds["infill"] * TIME_EFFICIENCY, profile.material["max_flow_mm3s"] / (lw * lh))
    return float(volume_mm3) / (lw * lh * v) / 60


def _estimate(layers: list[Layer], profile: Profile) -> dict:
    """Rough print time and filament use, with the walls / infill / layer height of the user's goal.
    Counts solid top/bottom skins and the slicer slowing down small layers to the material's
    minimum layer time. ``layer_times`` are the unslowed times (for the cooling check)."""
    plan = goal_plan(profile)
    lw, lh = profile.line_width, plan["layer_height"]
    walls, infill = plan["walls"], plan["infill"] / 100
    sp = profile.speeds
    # At the melt-rate limit the head runs slower and steadier, so the efficiency doesn't apply on top.
    flow_speed = profile.material["max_flow_mm3s"] / (lw * lh)  # mm/s at the volumetric limit
    v_wall = min(sp["outer_wall"] * TIME_EFFICIENCY, flow_speed)
    v_inf = min(sp["infill"] * TIME_EFFICIENCY, flow_speed)
    min_layer = profile.material["min_layer_time_s"]
    zs = np.array([layer.z for layer in layers])
    times, volume, total = [], 0.0, 0.0
    for i, layer in enumerate(layers):
        g = layer.geom
        if g.is_empty:
            times.append(0.0)
            continue
        solid = _skin_area(layers, zs, i, plan["top"] * lh, plan["bottom"] * lh)
        wall_area = min(g.area, g.length * walls * lw)
        core = max(g.area - wall_area, 0.0)
        solid = min(solid, core)
        # thin walls fit fewer loops than asked for: time follows the wall area that's really printed
        t = wall_area / lw / v_wall + (solid + (core - solid) * infill) / lw / v_inf + LAYER_OVERHEAD_S
        times.append(t)
        real_layers = layer.dz / lh
        total += max(t, min_layer) * real_layers
        volume += (wall_area + solid + (core - solid) * infill) * lh * real_layers
    grams = volume / 1000 * profile.material["density_g_cm3"]
    return {"print_time_min": START_MIN + total / 60, "filament_g": grams, "layer_times": times}


def _skin_area(layers: list[Layer], zs: np.ndarray, i: int, top: float, bottom: float) -> float:
    """Area of layer i printed solid: what it has more than the layer ``top`` mm above or ``bottom``
    mm below (none there = outside the part: all skin). Area differences, not polygon overlays:
    near-coincident walls of two layers make Pyodide's older GEOS abort, and a time estimate
    doesn't need the exact shape."""
    area = layers[i].geom.area
    exposed = 0.0
    for z, step in ((zs[i] + top, 1), (zs[i] - bottom, -1)):
        if not 0 <= z <= zs[-1] + 1e-6:
            return area  # the first/last few layers are all skin
        j = int(np.argmin(np.abs(zs - z)))
        j = j if j != i else min(max(i + step, 0), len(layers) - 1)  # sampled layers further apart than the skin
        exposed = max(exposed, area - layers[j].geom.area)
    return exposed


def _layer_heat(layers: list[Layer], profile: Profile, sliced: dict | None = None, **kw) -> np.ndarray:
    """Temperature just below the top of each layer while printing (see heat.py)."""
    zs = np.array([l.z for l in layers])
    areas = np.array([l.geom.area for l in layers])
    perimeters = np.array([l.geom.length for l in layers])
    return layer_heat(zs, areas, perimeters, profile, sliced, **kw)


def _cooling_findings(layers, heat: np.ndarray, profile: Profile, T: dict, sliced: dict | None = None) -> list[dict]:
    """Layers still soft when more is printed on top (heat build-up): small tips and pins droop or blob."""
    soft = profile.material["softening_c"]
    C = T["cooling"]
    hot = lambda temps: [(layer, temp) for layer, temp in zip(layers, temps) if layer.z > C["above_bed_mm"] and temp >= soft + C["soft_margin_c"]]
    if not hot(heat):
        return []
    # The slicer slows small layers to the minimum layer time, but not below the filament's minimum print
    # speed: a thin pin gets far less time than promised. Find the highest minimum speed that cools them
    # (estimated level only: with a G-code the real layer times are known already).
    fix = None
    if sliced is None:
        own = profile.material["min_layer_time_s"]
        tries = [(v, own) for v in C["try_min_speeds"]] + [(C["try_min_speeds"][-1], t) for t in C["try_layer_times_s"] if t > own]
        fix = next(((v, t) for v, t in tries if not hot(_layer_heat(layers, profile, min_speed=v, min_layer_time=t))), 0)
    out, run = [], []
    for layer, temp in zip(layers, heat):
        if layer.z > C["above_bed_mm"] and temp >= soft + C["soft_margin_c"]:
            run.append((layer, temp))
            continue
        out += _cooling_run(run, profile, C, fix)
        run = []
    return out + _cooling_run(run, profile, C, fix)


def _cooling_run(run, profile: Profile, C: dict, fix: tuple | int | None = None) -> list[dict]:
    if not run or run[-1][0].z - run[0][0].z < C["min_run_mm"]:
        return []
    layers = [l for l, _ in run]
    hottest = max(t for _, t in run)
    soft = profile.material["softening_c"]
    b = np.array([l.geom.bounds for l in layers])
    bbox = [[b[:, 0].min(), b[:, 1].min(), layers[0].z], [b[:, 2].max(), b[:, 3].max(), layers[-1].z]]
    sev = "warn" if hottest >= soft + C["hot_margin_c"] else "info"
    detail = (f"At {layers[0].z:.0f}–{layers[-1].z:.0f} mm the plastic is still about {hottest:.0f} °C when the next layers go on "
              f"({profile.material['name']} softens from about {soft:.0f} °C) — thin parts here can droop, blob or bend")
    extra = {}
    if fix:
        speed, layer_time = fix
        longer = layer_time > profile.material["min_layer_time_s"]
        detail += (f". In the filament's cooling settings set the minimum print speed to {speed:g} mm/s"
                   + (f" and the layer time to {layer_time:g} s" if longer else "")
                   + f" (OrcaSlicer: Min print speed{', Layer time' if longer else ''}), so the slicer slows these layers enough to cool")
        extra.update(min_speed_mm_s=speed, min_layer_time_s=layer_time)
    elif fix == 0:
        detail += ". Slowing down alone won't cool them: print two copies at once so each layer takes longer"
    return [_finding("small_layers_cooling", sev, detail, bbox=bbox, temp_c=hottest, softening_c=soft, **extra)]


def _profile_findings(layers, profile: Profile) -> list[dict]:
    out = []
    mat, prn = profile.material, profile.printer
    if mat["warp_risk"] != "low" and profile.enclosure == "none":
        out.append(_finding("enclosure_recommended", "warn" if mat["warp_risk"] == "high" else "info",
                            f"{mat['name']} on an open printer ({prn['name']}): expect warping and weaker layers"))
    if mat["abrasive"] and not prn["hardened_nozzle"]:
        out.append(_finding("hardened_nozzle_required", "problem",
                            f"{mat['name']} wears out the standard {prn['name']} nozzle — fit a hardened one first"))
    if mat["hygroscopic"] != "low":
        out.append(_finding("dry_filament", "warn" if mat["hygroscopic"] == "high" else "info",
                            f"{mat['name']} absorbs moisture — wet filament prints stringy, bubbly and weak"))
    return out
