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
from shapely.geometry import Polygon

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
    "trapped_support": "fails",
    "hardened_nozzle_required": "fails",
    "warping": "shape",
    "elephant_foot": "shape",
    "first_layer_detail": "shape",
    "small_hole_undersized": "shape",
    "bridge_sag": "shape",
    "hole_starts_midair": "shape",
    "sideways_hole_oval": "shape",
    "long_bridge": "shape",
    "small_layers_cooling": "shape",
    "tall_thin_feature": "shape",
    "support_marks": "surface",
    "fine_detail_rough": "surface",
    "dry_filament": "surface",
    "weak_upright_pin": "weak",
    "thin_neck": "weak",
    "single_line_wall": "weak",
    "enclosure_recommended": "shape",
    "turned_to_fit": "info",
    "shrinkage_size_error": "shape",
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


def run_checks(mesh: Mesh, profile: Profile, max_layers: int = 160) -> dict:
    """Slice an oriented mesh and run all checks. Returns findings + stats."""
    T = load_data("thresholds")
    layers = slice_mesh(mesh, profile.layer_height, max_layers)
    tracker = Tracker(link=2 * profile.line_width)
    _scan_layers(layers, profile, tracker, T)

    findings: list[dict] = []
    findings += _support_findings(layers, tracker, profile, T)
    findings += _feature_findings(tracker, profile, T)
    findings += _hole_findings(tracker, profile, T)
    findings = _sideways_hole_findings(mesh, profile, T, findings)
    findings += _first_layer_findings(layers, profile, T)
    findings += _bed_findings(layers, profile, T)
    findings += _weak_findings(layers, tracker, profile, T)
    stats = _estimate(layers, profile)
    findings += _cooling_findings(layers, stats["layer_times"], profile, T)
    findings += _profile_findings(layers, profile)
    findings += _shrinkage_findings(mesh, profile, T)

    for f in findings:
        f["impact"] = RULE_IMPACT.get(f["rule"], "info")
    findings.sort(key=lambda f: (IMPACT_ORDER.index(f["impact"]), SEVERITY_ORDER[f["severity"]]))
    del stats["layer_times"]
    stats.update(
        layers_analysed=len(layers),
        height_mm=float(mesh.triangles[..., 2].max()),
        support_area_mm2=float(sum(r.metrics.get("area", 0) for r in tracker.of("overhang"))),
        needs_support=any(r.volume >= 2.0 for r in tracker.of("overhang")),
    )
    return {"findings": findings, "stats": stats, "z_strength_factor": profile.z_strength_factor, "material": profile.material["name"]}


# ---------------------------------------------------------------- layer scan


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
            reach = layer.dz * tan_lim + 0.5 * lw
            anchor_zone = prev.buffer(reach + lw, quad_segs=QUAD)  # must reach past the supported zone
            for piece in parts(_safe_diff(g, prev.buffer(reach, quad_segs=QUAD)), min_piece):
                span, anchors = _bridge_span(piece, anchor_zone, max_bridge)
                holes = _open_holes(piece, g)
                if holes:
                    tracker.add("hole_start", piece, layer, hole_d=max(holes))
                if anchors >= 2:
                    tracker.add("bridge", piece, layer, span=span, area=piece.area)
                else:
                    tracker.add("overhang", piece, layer, area=piece.area, enclosed=float(_is_enclosed(piece, g)))
        # Width/hole/pin analysis only depends on this layer's shape: reuse it while the shape repeats.
        features = cached[1] if same else _layer_features(g, lw, vanish, rough, single, G, H, T["pins"])
        cached = (key if not same else cached[0], features, g)
        for kind, piece, metrics in features:
            tracker.add(kind, piece, layer, **metrics)
        prev = g


def _layer_features(g, lw, vanish, rough, single, G, H, P) -> list[tuple]:
    """(kind, piece, metrics) for one layer: thin features, narrow gaps, round holes, slender islands."""
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
    # Round vertical holes (interior rings), for size checks.
    for poly in parts(g):
        for ring in poly.interiors:
            hole = Polygon(ring)
            d = 2 * np.sqrt(hole.area / np.pi)
            if d <= H["undersize_up_to_mm"] and 4 * np.pi * hole.area / max(hole.length, 1e-9) ** 2 >= H["circularity_min"]:
                out.append(("hole", hole, {"min_d": d}))
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


def _bridge_span(piece: Polygon, anchor_zone, max_span: float = np.inf) -> tuple[float, int]:
    """Number of separate anchors on the layer below, and the largest gap between them.

    A piece held all the way round (roof of a pocket, ring over a counterbore) is one closed anchor;
    it bridges across its narrow width, so it counts as two anchors that far apart when that width is
    at most ``max_span`` (wider, the slicer supports it instead)."""
    touch = piece.boundary.intersection(anchor_zone)
    if touch.is_empty:
        return 0.0, 0
    merged = shapely.line_merge(touch) if touch.geom_type == "MultiLineString" else touch
    anchors = [a for a in getattr(merged, "geoms", [merged]) if a.length > 0.05]
    if len(anchors) < 2:
        if anchors and piece.exterior.intersection(anchor_zone).length >= 0.9 * piece.exterior.length:
            width = _min_width(Polygon(piece.exterior))
            if width <= max_span:
                return width, 2
        return 0.0, len(anchors)
    span = max(min(a.distance(b) for b in anchors if b is not a) for a in anchors)
    return float(span), len(anchors)


def _safe_diff(a, b):
    if a.is_empty or b.is_empty:
        return a
    try:
        return a.difference(b)
    except Exception:
        try:
            return shapely.set_precision(a, 1e-3).difference(shapely.set_precision(b, 1e-3))
        except Exception:
            try:
                return shapely.make_valid(a).difference(shapely.make_valid(b))
            except Exception:
                return Polygon()


def _opening(g, r: float):
    """Morphological opening: removes everything narrower than 2r."""
    b = g.buffer(-r, quad_segs=QUAD)
    if b.is_empty:
        return Polygon()
    return b.buffer(r * 1.05, quad_segs=QUAD)


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
    limit = W["risk_length_mm"].get(profile.material_id, {}).get(profile.enclosure)
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


def _runs(idx: list[int]) -> list[list[int]]:
    runs: list[list[int]] = []
    for i in idx:
        if runs and i == runs[-1][-1] + 1:
            runs[-1].append(i)
        else:
            runs.append([i])
    return runs


# ---------------------------------------------------------------- time, cooling, setup


def _estimate(layers: list[Layer], profile: Profile) -> dict:
    """Very rough print time and filament use, with the walls / infill / layer height of the user's goal."""
    plan = goal_plan(profile)
    lw, lh = profile.line_width, plan["layer_height"]
    walls, infill = plan["walls"], plan["infill"] / 100
    sp = profile.speeds
    flow_speed = profile.material["max_flow_mm3s"] / (lw * lh)  # mm/s at the volumetric limit
    # Real moves rarely reach nominal speed (acceleration, short segments, travel).
    efficiency, layer_overhead_s = 0.4, 1.5
    v_wall = min(sp["outer_wall"], flow_speed) * efficiency
    v_inf = min(sp["infill"], flow_speed) * efficiency
    times, volume, total = [], 0.0, 0.0
    for layer in layers:
        g = layer.geom
        perim = g.length
        wall_area = min(g.area, perim * walls * lw)
        core = g.area - wall_area
        t = perim * walls / v_wall + core * infill / lw / v_inf + layer_overhead_s
        times.append(t)
        real_layers = layer.dz / lh
        total += t * real_layers
        volume += (wall_area + core * infill) * lh * real_layers
    grams = volume / 1000 * profile.material["density_g_cm3"]
    return {"print_time_min": total / 60 * 1.15, "filament_g": grams, "layer_times": times}


def _cooling_findings(layers, times, profile: Profile, T: dict) -> list[dict]:
    """Short layers in a part whose other layers are much bigger: a local hot spot that deforms.
    (A part that is small everywhere is handled by the slicer's minimum layer time.)"""
    limit = profile.material["min_layer_time_s"]
    max_area = max(l.geom.area for l in layers)
    small = T["cooling"]["small_layer_area_mm2"]
    out, run = [], []
    for layer, t in zip(layers, times):
        if 0 < t < limit and layer.index > 0 and layer.geom.area < min(0.25 * max_area, small):
            run.append(layer)
            continue
        out += _cooling_run(run, profile)
        run = []
    return out + _cooling_run(run, profile)


def _cooling_run(run, profile) -> list[dict]:
    if not run or run[-1].z - run[0].z < 2.0:
        return []
    b = np.array([l.geom.bounds for l in run])
    bbox = [[b[:, 0].min(), b[:, 1].min(), run[0].z], [b[:, 2].max(), b[:, 3].max(), run[-1].z]]
    return [_finding("small_layers_cooling", "info",
                     f"Small layers at {run[0].z:.1f}–{run[-1].z:.1f} mm get little time to cool — tips can droop or blob",
                     bbox=bbox)]


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
