"""Printability checks on a sliced, oriented part.

Each check emits findings: ``{rule, severity, detail, bbox, metrics}``. ``rule``
points into ``data/rules.yaml`` for the explanation and fixes; ``detail`` is the
part-specific sentence ("8 mm bridge at 23 mm height"). Severities:
``problem`` (likely to fail), ``warn`` (likely to look bad / be weak), ``info``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import shapely
from shapely.geometry import Polygon

from .mesh import Mesh
from .profile import Profile
from .slicer import Layer, parts, slice_mesh

QUAD = 2  # buffer resolution; coarse is fine and much faster


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
    """Links 2D pieces on consecutive layers into regions."""

    def __init__(self, link: float):
        self.link = link
        self.regions: list[Region] = []

    def add(self, kind: str, piece: Polygon, layer: Layer, **metrics) -> Region:
        for r in self.regions:
            if r.kind == kind and r.last_index == layer.index - 1 and r.last.distance(piece) <= self.link:
                r.add(piece, layer)
                for k, v in metrics.items():
                    r.metrics[k] = max(r.metrics.get(k, v), v)
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
    lw, lh = profile.line_width, profile.layer_height
    mat = profile.material
    layers = slice_mesh(mesh, lh, max_layers)
    tracker = Tracker(link=2 * lw)
    findings: list[dict] = []

    _check_layers(layers, profile, tracker)
    findings += _support_findings(tracker, profile)
    findings += _thin_findings(tracker, profile)
    findings += _bed_findings(layers, profile)
    findings += _tall_thin_findings(tracker, profile)
    stats = _estimate(layers, profile)
    findings += _cooling_findings(layers, stats["layer_times"], profile)
    findings += _profile_findings(layers, profile, stats, tracker)

    order = {"problem": 0, "warn": 1, "info": 2}
    findings.sort(key=lambda f: order[f["severity"]])
    del stats["layer_times"]
    stats.update(layers_analysed=len(layers), height_mm=float(mesh.triangles[..., 2].max()))
    return {"findings": findings, "stats": stats, "z_strength_factor": profile.z_strength_factor, "material": mat["name"]}


def _check_layers(layers: list[Layer], profile: Profile, tracker: Tracker):
    lw = profile.line_width
    tan_lim = np.tan(np.radians(profile.material["max_overhang_deg"]))
    min_piece = max(1.0, 4 * lw * lw)
    prev = None
    for layer in layers:
        g = layer.geom
        if g.is_empty:
            prev = g
            continue
        # Unsupported area: whatever reaches further out than the overhang limit allows.
        if prev is not None:
            reach = layer.dz * tan_lim + 0.5 * lw
            supported = prev.buffer(reach, quad_segs=QUAD) if not prev.is_empty else prev
            # Must reach past the supported zone, or a bridge never touches its anchors.
            anchor_zone = prev.buffer(reach + lw, quad_segs=QUAD) if not prev.is_empty else prev
            for piece in parts(g.difference(supported), min_piece):
                span, anchors = _bridge_span(piece, anchor_zone)
                kind = "bridge" if anchors >= 2 else "overhang"
                tracker.add(kind, piece, layer, span=span, area=piece.area)
        # Thin walls: what a morphological opening with radius r removes is narrower than 2r.
        too_thin = g.difference(_opening(g, lw / 2))
        thin = g.difference(_opening(g, lw)).difference(too_thin.buffer(0.01))
        for kind, geom in (("too_thin", too_thin), ("thin", thin)):
            for piece in parts(geom, max(lw * 1.5, 0.5)):
                width = 2 * piece.area / max(piece.length, 1e-9)  # strip of width w: A = wL, P ≈ 2L
                tracker.add(kind, piece, layer, width=width)
        # Slender islands, for tall-thin detection.
        for island in parts(g, 0.01):
            w, length = _rect_sides(island)
            if w < 5.0 and length < 15.0:  # pin or post; long thin walls are thin_wall's job
                tracker.add("slender", island, layer, width=w)
        prev = g


def _bridge_span(piece: Polygon, anchor_zone) -> tuple[float, int]:
    """Number of separate anchors on the layer below, and the largest gap between them."""
    if anchor_zone.is_empty:
        return 0.0, 0
    touch = piece.boundary.intersection(anchor_zone)
    if touch.is_empty:
        return 0.0, 0
    merged = shapely.line_merge(touch) if touch.geom_type == "MultiLineString" else touch
    anchors = [a for a in getattr(merged, "geoms", [merged]) if a.length > 0.05]
    if len(anchors) < 2:
        return 0.0, len(anchors)
    span = max(min(a.distance(b) for b in anchors if b is not a) for a in anchors)
    return float(span), len(anchors)


def _opening(g, r: float):
    """Morphological opening: removes everything narrower than 2r."""
    return g.buffer(-r, quad_segs=QUAD).buffer(r * 1.05, quad_segs=QUAD)


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


def _support_findings(tracker: Tracker, profile: Profile) -> list[dict]:
    out = []
    max_bridge = profile.material["max_bridge_mm"]
    for r in tracker.of("overhang"):
        if r.volume < 2.0:  # specks from round edges
            continue
        out.append(_finding("unsupported_overhang", "problem",
                            f"Overhang without support at {_zrange(r)} height", r, profile))
    for r in tracker.of("bridge"):
        span = r.metrics["span"]
        if span <= 2 * profile.line_width:  # not a real gap
            continue
        if span > max_bridge:
            out.append(_finding("long_bridge", "warn",
                                f"{span:.0f} mm bridge at {r.z0:.1f} mm (comfortable for {profile.material['name']}: ≤ {max_bridge} mm)", r, profile))
        else:
            out.append(_finding("short_bridge", "info", f"{span:.0f} mm bridge at {r.z0:.1f} mm — usually prints fine", r, profile))
    return out


def _thin_findings(tracker: Tracker, profile: Profile) -> list[dict]:
    out = []
    lw = profile.line_width
    for kind, rule, sev in (("too_thin", "too_thin_to_print", "problem"), ("thin", "thin_wall", "warn")):
        for r in tracker.of(kind):
            if r.z1 - r.z0 < 2 * profile.layer_height and r.volume < 1.0:
                continue
            out.append(_finding(rule, sev, f"Section about {r.metrics['width']:.2f} mm thin ({r.metrics['width'] / lw:.1f} line widths) "
                                f"at {_zrange(r)}", r, profile))
    return out


def _bed_findings(layers: list[Layer], profile: Profile) -> list[dict]:
    out = []
    first = layers[0].geom
    mat = profile.material
    if first.is_empty:
        return [_finding("small_bed_contact", "problem", "Nothing touches the bed in this orientation", bbox=None)]
    height = layers[-1].z
    max_area = max(l.geom.area for l in layers)
    b = first.bounds
    bed_bbox = [[b[0], b[1], 0.0], [b[2], b[3], profile.layer_height]]

    if first.area < 0.15 * max_area or first.area < 40:
        out.append(_finding("small_bed_contact", "warn" if first.area < 40 else "info",
                            f"Only {first.area:.0f} mm² touches the bed (largest layer {max_area:.0f} mm²)",
                            bbox=bed_bbox, bed_area=first.area))
    width = _min_width(first.convex_hull)
    if width > 0 and height / width > 4:
        out.append(_finding("tipping_risk", "warn", f"{height:.0f} mm tall on a base only {width:.0f} mm wide", bbox=bed_bbox))

    corners = _sharp_corners(first)
    if corners:
        sev = {"high": "warn", "medium": "warn", "low": "info"}[mat["warp_risk"]]
        pts = np.array(corners)
        out.append(_finding("sharp_bed_corners", sev,
                            f"{len(corners)} sharp corner(s) on the bed — these lift first with {mat['name']}",
                            bbox=[[*pts.min(axis=0), 0.0], [*pts.max(axis=0), profile.layer_height]], points=corners))
    span = max(b[2] - b[0], b[3] - b[1])
    if mat["warp_risk"] != "low" and span > 80:
        out.append(_finding("warping_material", "warn" if mat["warp_risk"] == "high" else "info",
                            f"{span:.0f} mm long footprint in {mat['name']} — large flat bases tend to warp", bbox=bed_bbox))
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


def _tall_thin_findings(tracker: Tracker, profile: Profile) -> list[dict]:
    out = []
    for r in tracker.of("slender"):
        h = r.z1 - r.z0
        w = r.metrics["width"]
        if h > 10 and h > 6 * w:
            out.append(_finding("tall_thin_feature", "warn", f"Slender feature ~{w:.1f} mm wide and {h:.0f} mm tall", r, profile))
    return out


def _estimate(layers: list[Layer], profile: Profile) -> dict:
    """Very rough print time and filament use."""
    lw, lh = profile.line_width, profile.layer_height
    walls, infill = 2, 0.15
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


def _cooling_findings(layers, times, profile: Profile) -> list[dict]:
    """Short layers in a part whose other layers are much bigger: a local hot spot.
    (A part that is small everywhere is handled by the slicer's minimum layer time.)"""
    limit = profile.material["min_layer_time_s"]
    max_area = max(l.geom.area for l in layers)
    out, run = [], []
    for layer, t in zip(layers, times):
        if 0 < t < limit and layer.index > 0 and layer.geom.area < 0.25 * max_area:
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
                     f"Layers at {run[0].z:.1f}–{run[-1].z:.1f} mm print in under {profile.material['min_layer_time_s']} s each",
                     bbox=bbox)]


def _profile_findings(layers, profile: Profile, stats: dict, tracker: Tracker) -> list[dict]:
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
                            f"{mat['name']} absorbs moisture — dry it before printing"))
    thin = tracker.of("thin") + tracker.of("too_thin")
    if profile.nozzle == "0.4" and stats["filament_g"] > 60 and not thin:
        out.append(_finding("larger_nozzle_option", "info",
                            f"About {stats['print_time_min'] / 60:.1f} h with a 0.4 mm nozzle; a 0.6 mm nozzle could save roughly a third"))
    if any(len(p.interiors) for p in parts(layers[0].geom)):
        out.append(_finding("elephant_foot", "info", "Holes touch the bed — the first layers squish slightly and can make them tight",
                            bbox=[[*layers[0].geom.bounds[:2], 0.0], [*layers[0].geom.bounds[2:], profile.layer_height]]))
    return out
