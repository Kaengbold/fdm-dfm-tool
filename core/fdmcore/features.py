"""Feature recognition (holes, bosses) and design edits ("ghost" geometry).

Features are fitted on curved regions from regions.py: a cylinder's normals are
all perpendicular to its axis, so the axis is the direction the normals avoid
(smallest eigenvector of Σ n nᵀ). Radius comes from a circle fit of the points
projected onto the plane across the axis.

Design edits are closed meshes in part coordinates that the web app cuts away
(``op: "cut"``) or adds (``op: "add"``, see additions.py) with manifold-3d (WASM),
then reloads the result. ``pieces`` says how many triangles each closed piece has.
"""

from __future__ import annotations

import numpy as np
import shapely
from shapely.geometry import LineString

from .checks import orient
from .mesh import Mesh
from .orientation import rotation_to_z
from .profile import load_data
from .slicer import slice_mesh

TEARDROP_MIN_DIAMETER = 5.0  # smaller horizontal holes bridge fine


# A concave arc covering at most this share of a circle is a rounded inside edge (a fillet: about a
# quarter), not a hole. A half-round slot end (one half) is still asked about (T-356).
ROUNDED_EDGE_COVERAGE = 0.4

TAGS = ("hole", "boss", "none")  # what the user can say a detected feature is ("none": not a feature)


def detect_features(mesh: Mesh, region: np.ndarray, tags: dict | None = None) -> list[dict]:
    """Round holes and bosses, one per curved region (``rounded_edge``: a fillet in an inside corner,
    listed so the user can still say it's a hole). ``tags`` ({region: tag}) are the user's
    answers: they override the guessed type, and "none" marks a false find (kept in the list,
    so it can be undone, but ignored by edits and checks). ``confidence`` "low" means: ask."""
    tags = {int(k): v for k, v in (tags or {}).items()}
    normals, areas = mesh.normals_and_areas()
    centroids = mesh.triangles.mean(axis=1)
    out = []
    for r, sel in _groups(region, min_size=6):
        n, a, c = normals[sel], areas[sel], centroids[sel]
        if np.linalg.norm((n * a[:, None]).sum(axis=0)) / a.sum() > 0.99:
            continue  # flat
        feat = _fit_cylinder(n, a, c, mesh.triangles[sel])
        if feat:
            feat["region"] = int(r)
            # Few faces or a partial circle (could be a rounded edge): worth asking.
            feat["confidence"] = "high" if feat["full_circle"] and len(sel) >= 12 and feat["fit_error"] < 0.02 else "low"
            tag = tags.get(int(r))
            feat["tag"] = tag
            if tag in ("hole", "boss"):
                feat["type"] = tag
            out.append(feat)
    return out


def _groups(region: np.ndarray, min_size: int = 1):
    """(region id, triangle indices) for every region with at least ``min_size`` triangles. CAD
    exports have thousands of regions: a mask per region would scan every triangle each time."""
    order = np.argsort(region)
    ids, starts, counts = np.unique(region[order], return_index=True, return_counts=True)
    for r, s, c in zip(ids, starts, counts):
        if c >= min_size:
            yield int(r), order[s:s + c]


def accepted(features: list[dict]) -> list[dict]:
    """Features the user hasn't rejected."""
    return [f for f in features if f.get("tag") != "none"]


def _fit_cylinder(n, a, c, tris) -> dict | None:
    w, v = np.linalg.eigh((n * a[:, None]).T @ n)
    axis = v[:, 0]
    if w[0] > 0.02 * w.sum():  # normals not perpendicular to one axis: not a cylinder
        return None
    pts = tris.reshape(-1, 3)
    u = np.cross(axis, [1.0, 0, 0] if abs(axis[0]) < 0.9 else [0, 1.0, 0])
    u /= np.linalg.norm(u)
    s = np.cross(axis, u)
    p2 = np.stack([pts @ u, pts @ s], axis=1)
    # Algebraic circle fit: x² + y² + Dx + Ey + F = 0
    A = np.column_stack([p2, np.ones(len(p2))])
    sol, *_ = np.linalg.lstsq(A, -(p2**2).sum(axis=1), rcond=None)
    cx, cy = -sol[0] / 2, -sol[1] / 2
    radius = float(np.sqrt(max(cx**2 + cy**2 - sol[2], 0)))
    resid = np.abs(np.linalg.norm(p2 - [cx, cy], axis=1) - radius)
    if radius <= 0 or resid.max() > 0.05 * radius + 0.05:
        return None
    t = pts @ axis
    center = cx * u + cy * s + axis * (t.min() + t.max()) / 2
    # Hole: normals point toward the axis. Coverage: how much of the circle exists.
    radial = c - (center + np.outer((c - center) @ axis, axis))
    inward = float(np.mean(np.einsum("ij,ij->i", n, radial) < 0))
    ang = np.sort(np.arctan2(p2[:, 1] - cy, p2[:, 0] - cx))
    coverage = 1.0 - float(np.diff(np.append(ang, ang[0] + 2 * np.pi)).max()) / (2 * np.pi)
    hole = inward > 0.5
    return {
        "type": ("rounded_edge" if coverage <= ROUNDED_EDGE_COVERAGE else "hole") if hole else "boss",
        "axis": axis.tolist(),
        "center": center.tolist(),
        "radius": radius,
        "diameter": 2 * radius,
        "length": float(t.max() - t.min()),
        "full_circle": coverage > 0.9,
        "fit_error": float(resid.max() / radius),
    }


# A boss (round post, often for a screw) much taller than it is wide snaps or wobbles at its foot: injection
# moulding practice is ribs / gussets once it is taller than about 3 × its outside diameter
# [Hubs boss design guide; research/cad_tricks_round2.md]. Printed, the ribs also spread the layers' load.
BOSS_TALL_RATIO = 3.0
BOSS_RIB_MM = 1.2  # rib thickness: about three printed lines
BOSS_RIB_HEIGHT = 0.6  # ribs reach this share of the boss's height


def _boss_base(mesh: Mesh, f: dict):
    """The end of boss ``f`` that stands on the part: (end point, direction into the boss, flat base triangles
    around it) or None when neither end has a flat face around it."""
    normals, areas = mesh.normals_and_areas()
    a, c, r = np.asarray(f["axis"], float), np.asarray(f["center"], float), f["radius"]
    cent = mesh.triangles.mean(axis=1)
    best = None
    for sign in (1.0, -1.0):
        end = c - sign * a * f["length"] / 2  # the boss rises from here in direction sign · a
        along = (cent - end) @ a
        radial = np.linalg.norm((cent - end) - np.outer(along, a), axis=1)
        base = (np.abs(along) < 0.2) & (normals @ (sign * a) > 0.99) & (radial > r)
        if base.any() and (best is None or areas[base].sum() > best[3]):
            best = (end, sign * a, np.flatnonzero(base), areas[base].sum())
    return None if best is None or best[3] < np.pi * r**2 else best[:3]


def _covered(mesh: Mesh, tris: np.ndarray, p: np.ndarray, normal: np.ndarray) -> bool:
    """``p`` lies on one of the flat triangles ``tris`` (all facing ``normal``)."""
    t = mesh.triangles[tris]
    v0, v1, v2 = t[:, 1] - t[:, 0], t[:, 2] - t[:, 0], p - t[:, 0]
    d00, d01, d11 = np.einsum("ij,ij->i", v0, v0), np.einsum("ij,ij->i", v0, v1), np.einsum("ij,ij->i", v1, v1)
    d20, d21 = np.einsum("ij,ij->i", v2, v0), np.einsum("ij,ij->i", v2, v1)
    den = np.where(np.abs(d00 * d11 - d01**2) < 1e-12, 1e-12, d00 * d11 - d01**2)
    v, w = (d11 * d20 - d01 * d21) / den, (d00 * d21 - d01 * d20) / den
    return bool(np.any((v >= -1e-6) & (w >= -1e-6) & (v + w <= 1 + 1e-6) & (np.abs(v2 @ normal) < 0.2)))


def _boss_ribs(mesh: Mesh, features: list[dict]) -> list[dict]:
    """Four triangular ribs round the foot of each boss taller than BOSS_TALL_RATIO × its diameter, on the face
    it stands on; shorter where that face ends (none where there's no room)."""
    from shapely.geometry import Polygon

    from .additions import extrude

    out = []
    for f in features:
        if f["type"] != "boss" or not f["full_circle"] or f["length"] < BOSS_TALL_RATIO * f["diameter"]:
            continue
        found = _boss_base(mesh, f)
        if found is None:
            continue
        end, d, base = found
        r, h = f["radius"], BOSS_RIB_HEIGHT * f["length"]
        w0 = np.cross(d, [1.0, 0, 0] if abs(d[0]) < 0.9 else [0, 1.0, 0])
        w0 /= np.linalg.norm(w0)
        pieces = []
        for k in range(4):
            w = w0 * np.cos(k * np.pi / 2) + np.cross(d, w0) * np.sin(k * np.pi / 2)
            reach = next((L for L in (h, 0.75 * h, 0.5 * h, 0.3 * h) if _covered(mesh, base, end + w * (r + L), d)), None)
            if reach is None or reach < 1.0:
                continue
            tri = extrude(Polygon([(r - 0.3, -0.3), (r + reach, -0.3), (r + reach, 0.0), (r - 0.3, h)]), -BOSS_RIB_MM / 2, BOSS_RIB_MM / 2)
            v = np.cross(w, d)
            placed = end + tri[..., :1] * w + tri[..., 1:2] * d + tri[..., 2:] * v
            pieces.append(placed if np.linalg.det(np.stack([w, d, v])) > 0 else placed[:, ::-1])
        if pieces:
            out.append(_edit(f"boss-ribs-{f['region']}", "tall_boss", f"{len(pieces)} ribs for the Ø{f['diameter']:.1f} mm boss",
                             f"The boss is {f['length']:.0f} mm tall, more than {BOSS_TALL_RATIO:g} × its width: ribs at its foot keep it "
                             "from snapping or wobbling there (and let it take a screw's load).", pieces, op="add"))
    return out


# Adjustable edit sizes: default, min, max (the UI shows these as inputs).
CORNER_ROUND_SHARE = 0.3  # low-warp materials: round corners once the footprint is this share of the warp length
CORNER_ARC_STEPS = 8
OFF_CORNER = 0.01  # mm cutters keep from the part's corner edges
MAX_FOOT_POINTS = 600  # outline points; more and the slicer's elephant foot compensation is the better tool
CHAMFER_REACH_VOXELS = 8  # the nearest inside edge this close to the weak spot (in analysis voxels) seeds the chamfer
HOT_SHARE = 0.65  # other inside corners get a chamfer too where the stress is at least this share of the highest
HOT_REACH_VOXELS = 3  # ... within this many analysis voxels of the corner
MAX_STRESS_CORNERS = 4
# A round as wide as gives the same plastic as the chamfer it replaces (square corner: c²/2 = r²(1 − π/4)).
# Just as wide, a round fills less than half as much and checked weaker (mushroom: 1.78 vs 1.92); with the
# same plastic both check the same (1.91 vs 1.92), and the round has no small corners for cracks to start
# at (rules.yaml stress_at_inside_corner).
ROUND_SAME_PLASTIC = float(np.sqrt(0.5 / (1 - np.pi / 4)))
MAX_FILLET_PIECES = 400

PARAMS = {
    "chamfer_mm": (3.0, 0.5, 10.0),  # radius of rounded bed corners
    "foot_chamfer_mm": (0.5, 0.2, 1.5),  # elephant-foot chamfer along the bottom edge
    "stress_chamfer_mm": (2.0, 0.5, 8.0),  # 45° chamfer at the inside corner where the strength check finds the most stress
    "teardrop_angle_deg": (45.0, 30.0, 60.0),  # tip half-angle from vertical; 45° is the classic choice
    "ledge_max_mm": (20.0, 2.0, 40.0),  # widest ledge to fill in at 45° underneath
    "wall_lines": (2.0, 2.0, 4.0),  # thin walls grow to this many printed lines
    "thicken_mm": (1.5, 0.5, 5.0),  # plastic added all round the weak spot from the strength check
}


def edit_params(params: dict | None) -> dict:
    """Defaults filled in and values clamped to their range."""
    out = {}
    for k, (default, lo, hi) in PARAMS.items():
        v = float((params or {}).get(k, default))
        out[k] = min(max(v, lo), hi)
    return out


def _edit(id_, rule, title, description, pieces: list[np.ndarray], op="cut", param=None) -> dict:
    """A design edit. An edit that both adds and cuts (grip fins) also gets ``piece_ops``, "add" or "cut" per
    piece; then, as for whole edits, every addition is made before any cut."""
    # A piece with a non-finite corner can't be built (the browser's manifold refuses it with an error, T-367):
    # leave it out rather than fail the whole change.
    pieces = [q for q in pieces if np.isfinite(q).all()]
    return {
        "id": id_, "rule": rule, "title": title, "description": description, "op": op, "param": param,
        "mesh": (np.concatenate(pieces) if pieces else np.zeros((0, 3, 3))).astype("<f4").tobytes(),
        "pieces": [len(q) for q in pieces],
    }


GROW_TOL_MM = 0.05  # ignore sub-0.05 mm noise when reporting outer-size growth


def _growth_text(grow: np.ndarray) -> str:
    """Plain-language note listing the axes that grow by more than GROW_TOL_MM."""
    items = [f"{g:.1f} mm in {ax}" if g >= 0.1 else f"{g:.2f} mm in {ax}"
             for g, ax in zip(grow, ("X", "Y", "Z")) if g > GROW_TOL_MM]
    joined = items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]
    return f"Outer size grows by {joined}: check what it fits into."


def _annotate_growth(edits: list[dict], mesh: Mesh) -> list[dict]:
    """Flag edits whose added geometry reaches outside the part's bounding box: attach ``grows_mm``
    and spell it out in plain words on the description so the part never grows silently."""
    pts = mesh.triangles.reshape(-1, 3)
    if not len(pts):
        return edits
    part_lo, part_hi = pts.min(axis=0), pts.max(axis=0)
    for e in edits:
        counts = e["pieces"]
        if not counts:
            continue
        ops = e.get("piece_ops") or [e["op"]] * len(counts)
        if not any(op == "add" for op in ops):
            continue
        tris = np.frombuffer(e["mesh"], "<f4").reshape(-1, 3, 3)
        add_chunks = []
        offset = 0
        for count, op in zip(counts, ops):
            if op == "add" and count:
                add_chunks.append(tris[offset:offset + count])
            offset += count
        if not add_chunks:
            continue
        add_pts = np.concatenate(add_chunks).reshape(-1, 3)
        grow = np.maximum(0.0, part_lo - add_pts.min(axis=0)) + np.maximum(0.0, add_pts.max(axis=0) - part_hi)
        if grow.max() > GROW_TOL_MM:
            e["grows_mm"] = [round(float(g), 2) if g > GROW_TOL_MM else 0.0 for g in grow]
            e["description"] = f"{e['description'].rstrip()} {_growth_text(grow)}"
    return edits


def design_edits(mesh: Mesh, region: np.ndarray, up, profile, params: dict | None = None, tags: dict | None = None,
                 weak_spot=None, voxel_mm: float = 1.0, fits: dict | None = None, gaps: dict | None = None,
                 hot_spots=None, fasteners: dict | None = None, strength_verdict: str | None = None) -> list[dict]:
    """Suggested geometry changes for this build direction, as closed meshes in part coordinates.
    ``weak_spot`` (part coordinates, from the last strength check) adds a chamfer at the nearest inside corner
    and thickens the weak stretch — only when ``strength_verdict`` is not ``"fine"`` (a part that is already
    strong enough shouldn't be thickened). ``hot_spots`` (points, share of the highest stress) extends those
    edits to the other inside corners and stretches that are nearly as stressed.
    ``fits`` ({region: {"partner_mm", "fit"}}) resizes holes and posts for what goes into them; ``gaps``
    ({(face a, face b): {"partner_mm", "fit", "warp_mm"}}) slots and tabs between two flat faces;
    ``fasteners`` ({region: {"size", "use"}}) holes for screws, inserts and nuts (see fastener_plan)."""
    from .additions import ledge_chamfers, magnet_slots, sacrificial_layers, thicken_walls

    up = np.asarray(up, float) / np.linalg.norm(up)
    prm = edit_params(params)
    angle = prm["teardrop_angle_deg"]
    edits = []
    for f in accepted(detect_features(mesh, region, tags)):
        axis = np.asarray(f["axis"])
        # Not for a screw / nut hole: it gets its own size (a teardrop at the old size would undo it).
        if (f["type"] == "hole" and f["full_circle"] and abs(axis @ up) < 0.2 and f["diameter"] >= TEARDROP_MIN_DIAMETER
                and f["region"] not in (fasteners or {})):
            edits.append(_edit(
                f"teardrop-{f['region']}", "short_bridge", f"Teardrop for the horizontal Ø{f['diameter']:.1f} mm hole",
                f"Makes the top of the hole a {angle:.0f}° point so it prints without sagging or support.",
                [_teardrop_cutter(f, up, angle)], param="teardrop_angle_deg"))
    edits += _fit_edits(accepted(detect_features(mesh, region, tags)), fits or {}, up, profile)
    edits += _grip_fins(mesh, accepted(detect_features(mesh, region, tags)), fits or {}, up, profile)
    edits += _gap_edits(mesh, region, gaps or {}, up, profile)
    edits += _fastener_edits(mesh, accepted(detect_features(mesh, region, tags)), fasteners or {}, up, profile)
    edits += _boss_ribs(mesh, accepted(detect_features(mesh, region, tags)))
    r = rotation_to_z(up)
    oriented = orient(mesh, r)
    to_part = _to_part(mesh, r)
    protect = _bed_protection(oriented, region, r, fits, gaps)
    corner_skipped: list[str] = []
    corner_edits = _corner_chamfers(oriented, to_part, profile, prm["chamfer_mm"], protect, corner_skipped)
    foot_skipped: list[str] = []
    foot_edits = _foot_chamfer(oriented, to_part, profile, prm["foot_chamfer_mm"], protect, foot_skipped)
    _attach_skip_notes(corner_edits, corner_skipped, foot_edits, foot_skipped)
    edits += corner_edits
    edits += foot_edits
    if strength_verdict != "fine":
        edits += _stress_chamfers(mesh, weak_spot, voxel_mm, prm["stress_chamfer_mm"], hot_spots, up,
                                  profile.material["max_overhang_deg"])
        edits += _thicker_weak_spot(mesh, weak_spot, prm["thicken_mm"], hot_spots, up)
    for i, c in enumerate(ledge_chamfers(oriented, profile, prm["ledge_max_mm"])):
        edits.append(_edit(
            f"ledge-{i}", "support_marks", f"45° fill under the {c['width']:.0f} mm ledge at {c['z']:.1f} mm",
            "Fills the corner under the overhang at 45°, so it prints without support (and without support marks).",
            [to_part(p) for p in c["pieces"]], op="add", param="ledge_max_mm"))
    for i, s in enumerate(sacrificial_layers(oriented, profile)):
        edits.append(_edit(
            f"sacrificial-{i}", "sacrificial_layer", f"Sacrificial layer over the Ø{s['diameter']:.1f} mm hole at {s['z']:.1f} mm",
            f"Closes the hole with one {profile.layer_height} mm layer where it starts in mid-air, so the layer below "
            "bridges cleanly. Poke or drill it open after printing.",
            [to_part(p) for p in s["pieces"]], op="add"))
    for i, m in enumerate(magnet_slots(oriented, profile)):
        x, y, z = m["size"]
        e = _edit(f"magnet-slot-{i}", "pause_to_insert", f"Side slot for the {x:.1f} × {y:.1f} × {z:.1f} mm pocket (no pause)",
                  f"Opens the closed pocket to the nearest side ({m['length_mm']:.1f} mm away), so the magnet or nut slides in after "
                  "printing instead of at a pause mid-print. Two small bumps at the pocket's mouth click it in place and keep "
                  "it there, without glue.", [to_part(p) for p in m["pieces"]], op="cut")
        e["piece_ops"] = m["ops"]
        edits.append(e)
    for i, w in enumerate(thicken_walls(oriented, profile, prm["wall_lines"])):
        edits.append(_edit(
            f"wall-{i}", "single_line_wall", f"Thicken the {w['width']:.2f} mm wall to {w['target']:.2f} mm",
            f"Grows the wall at {w['z0']:.0f}–{w['z1']:.0f} mm to {prm['wall_lines']:.0f} printed lines so it prints solid and is "
            "much stronger. It grows on both sides; check that nothing needs to fit there.",
            [to_part(p) for p in w["pieces"]], op="add", param="wall_lines"))
    return _annotate_growth(edits, mesh)


# Which check findings each kind of design change (its ``rule``) fixes. A change is offered as a fix only when
# one of them is in the current check, near the change; the rest are folded away as "Other ideas" (T-432).
EDIT_FIXES = {
    "short_bridge": ("sideways_hole_oval", "bridge_sag", "long_bridge", "trapped_support"),
    "sharp_bed_corners": ("warping",),
    "elephant_foot": ("elephant_foot",),
    "support_marks": ("support_marks",),
    "sacrificial_layer": ("hole_starts_midair",),
    "pause_to_insert": ("pause_to_insert",),
    "single_line_wall": ("single_line_wall", "too_thin_to_print"),
    "tall_boss": ("tall_thin_feature", "weak_upright_pin"),
}
ASKED_FOR = {"fit_size", "fastener", "press_fit_fins", "split"}  # sizes the user typed in: their own request
FROM_STRENGTH = {"stress_at_inside_corner", "thin_neck"}  # from the weak spot of the last strength check
NEAR_MM = 2.0  # a change this close to a finding's box is at that finding


def link_to_findings(edits: list[dict], findings: list[dict], to_print) -> list[dict]:
    """Say what each edit is for, in ``source``: "finding" (with ``fixes``: the finding's ``rule`` and ``title``),
    "request" (a size the user asked for), "strength" (the strength check's weak spot) or "other" (no
    finding asks for it: shown folded as "Other ideas"; ``why_other`` says why when there's more to say). ``findings`` as check returns
    them (print frame); ``to_print`` maps part-frame points (n × 3) to that frame."""
    T = load_data("thresholds")["first_layer"]
    for e in edits:
        e["fixes"] = None
        if e["rule"] in ASKED_FOR:
            e["source"] = "request"
            continue
        if e["rule"] in FROM_STRENGTH:
            e["source"] = "strength"
            continue
        e["source"] = "other"
        e["why_other"] = None
        wanted = EDIT_FIXES.get(e["rule"], ())
        pts = np.frombuffer(e["mesh"], "<f4").reshape(-1, 3)
        box = None
        if len(pts):
            p = np.asarray(to_print(pts.astype(float)))
            box = (p.min(axis=0) - NEAR_MM, p.max(axis=0) + NEAR_MM)
        match = next((f for f in findings if f["rule"] in wanted and _overlaps(box, f.get("bbox"))), None)
        if match is None:
            continue
        if e["rule"] == "elephant_foot" and T["elephant_foot_compensation_mm"] >= T["elephant_foot_mm"][1]:
            e["why_other"] = "Your slicer's elephant foot compensation (set in the 3MF) already takes up the bulge."
            continue
        e["source"] = "finding"
        e["fixes"] = {"rule": match["rule"], "title": match.get("title", match["rule"])}
        e.pop("why_other")
        if e["rule"] == "elephant_foot":
            lo, hi = T["elephant_foot_mm"]
            e["description"] += (f" Your slicer's elephant foot compensation (set in the 3MF) pulls the first layer in by "
                                 f"{T['elephant_foot_compensation_mm']:g} mm, but the bulge can reach {hi:g} mm: the chamfer takes "
                                 "the rest, where holes and edges at the bed must be exact.")
    return edits


def _overlaps(box, bbox) -> bool:
    """Does the edit's box (lo, hi) touch a finding's bbox? A finding without a place (the whole part) always does."""
    if not bbox or box is None:
        return True
    lo, hi = np.asarray(bbox[0], float), np.asarray(bbox[1], float)
    return bool(np.all(box[0] <= hi) and np.all(lo <= box[1]))


FIT_NAMES = {"press": "be pressed in (stays put)", "snug": "push in snugly", "sliding": "slide and turn freely", "loose": "drop in loosely"}


def fit_size(feature: dict, partner_mm: float, fit: str, up, profile=None, warp_mm: float = 0.0) -> tuple[float, str]:
    """Diameter to model a hole (or round post) so a ``partner_mm`` part fits it the ``fit`` way,
    and why, in plain words. ``warp_mm``: how much the warping simulation says the fit tightens."""
    F = load_data("thresholds")["fits"]
    if fit not in F["gap_mm"]:
        raise ValueError(f"fit must be one of {tuple(F['gap_mm'])}")
    up = np.asarray(up, float) / np.linalg.norm(up)
    sideways = abs(np.asarray(feature["axis"]) @ up) < 0.7
    measured = (profile.material.get("fit_gap_mm") or {}) if profile is not None else {}  # from the fit test print
    gap = measured.get(fit, F["gap_mm"][fit]) + (F["sideways_extra_mm"] if sideways else 0.0) + max(warp_mm, 0.0)
    hole = feature["type"] == "hole"
    size = partner_mm + gap if hole else partner_mm - gap
    what = (f"For a Ø{partner_mm:g} mm part to {FIT_NAMES[fit]}, model this hole Ø{size:.2f} mm" if hole else
            f"For this post to {FIT_NAMES[fit]} into a Ø{partner_mm:g} mm hole, model it Ø{size:.2f} mm")
    why = f" (now Ø{feature['diameter']:.2f}: {size - feature['diameter']:+.2f} mm)."
    if sideways:
        why += " It lies on its side when printed, so it comes out a little smaller at the top: that's included."
    if warp_mm > 0.02:
        why += f" The warping simulation shows it squeezing by about {warp_mm:.2f} mm as the part curls (rough): added."
    err = profile.material.get("hole_error_mm" if hole else "outer_error_mm") if profile is not None else None
    note = (" Using the gaps from your fit test print." if measured.get(fit) is not None
            else f" Your measured {'hole' if hole else 'outer size'} error ({err:+.2f} mm) is corrected by the "
                 f"{'hole' if hole else 'outer size'} compensation in the 3MF: keep it on when you slice." if err
            else " Printers differ by a tenth or two: the fit test print, or a hole you measured (My materials), gives your printer's numbers.")
    return size, what + why + note


def _fit_edits(features: list[dict], fits: dict, up, profile=None) -> list[dict]:
    """Resize holes and posts the user said something fits into: a ring cut away or added."""
    F = load_data("thresholds")["fits"]
    out = []
    for f in features:
        want = fits.get(f["region"])
        if not want:
            continue
        size, why = fit_size(f, want["partner_mm"], want["fit"], up, profile, want.get("warp_mm", 0.0))
        change = size - f["diameter"]
        if abs(change) < F["min_change_mm"]:
            continue
        r_old, r_new = f["radius"], size / 2
        hole = f["type"] == "hole"
        grow_part = (change < 0) == hole  # a smaller hole or a bigger post means adding plastic
        overlap = 0.3  # reach into the existing plastic so the pieces join cleanly
        if hole:
            inner, outer = (r_new, r_old + overlap) if grow_part else (0.0, r_new)
        else:
            inner, outer = (r_old - overlap, r_new) if grow_part else (r_new, r_old + overlap)
        name = "hole" if hole else "post"
        out.append(_edit(f"fit-{f['region']}", "fit_size", f"Ø{f['diameter']:.2f} → Ø{size:.2f} mm {name} for a {want['fit']} fit",
                         why, [_ring(f, inner, outer, grow_part)], op="add" if grow_part else "cut"))
    return out


def _fin_shapes(r_tip: float, r_pocket: float, count: int, thick: float, angle_deg: float):
    """Grip fins in the hole frame: ``count`` straight fins ``thick`` wide, from the pocket wall (r_pocket, a
    little into it) in to r_tip, each leaning ``angle_deg`` off the radial line so it bends like a leaf spring."""
    from shapely.geometry import LineString, Point

    a = np.radians(angle_deg)
    reach = r_pocket + 0.3
    fins = []
    for k in range(count):
        phi = 2 * np.pi * k / count
        tip = r_tip * np.array([np.cos(phi), np.sin(phi)])
        d = np.array([np.cos(phi + a), np.sin(phi + a)])
        s = -tip @ d + np.sqrt((tip @ d) ** 2 - tip @ tip + reach**2)  # tip + s·d lands on the circle r = reach
        # From a little short of the tip circle (the tilted end's corner reaches in further), then trimmed at it.
        fin = LineString([tip - d * thick, tip + s * d]).buffer(thick / 2, cap_style="flat")
        fins.append(fin.intersection(Point(0, 0).buffer(reach, 64)).difference(Point(0, 0).buffer(r_tip, 64)))
    return fins


def _grip_fins(mesh: Mesh, features: list[dict], fits: dict, up, profile=None) -> list[dict]:
    """For a hole something is pressed into: an open pocket with angled fins whose tips reach past the
    partner's size (``grip_fins`` in thresholds.yaml). They bend as it goes in, so the grip stays when the
    printer is off by a tenth or two, where a rigid press fit goes loose or cracks the part. Only for holes
    printed standing (fins on their side would be overhanging plates) with room in the wall for the pocket."""
    from shapely.geometry import Point
    from shapely.ops import unary_union

    from .additions import _triangulate

    G = load_data("thresholds")["grip_fins"]
    up = np.asarray(up, float) / np.linalg.norm(up)
    lw = profile.line_width if profile is not None else 0.45
    out = []
    for f in features:
        want = fits.get(f["region"])
        if not want or want["fit"] != "press" or f["type"] != "hole" or not f["full_circle"] or abs(np.asarray(f["axis"]) @ up) < 0.9:
            continue
        r_tip = want["partner_mm"] / 2 - G["interference_mm"]
        thick = max(G["thickness_mm"], 2 * lw)  # two lines: the slicer prints each fin as one folded loop
        wall = hole_wall(mesh, f)
        room = (wall if wall is not None else 0.0) - G["min_wall_mm"]  # how far the pocket may reach past the hole
        length = min(G["length_mm"], (f["radius"] + room - r_tip) / np.cos(np.radians(G["angle_deg"])))
        if length < G["min_length_mm"]:
            continue
        r_pocket = r_tip + length * np.cos(np.radians(G["angle_deg"]))
        count = next(n for d, n in G["count_by_diameter"] if want["partner_mm"] <= d)
        fins = _fin_shapes(r_tip, r_pocket, count, thick, G["angle_deg"])
        pocket = Point(0, 0).buffer(r_pocket, 64).difference(unary_union(fins))
        add_ends, cut_ends = _ends(f, True), _ends(f, False)
        pieces = [_axis_prism(f, fin, *add_ends) for fin in fins]
        # The fins' arcs against the pocket's circle can stump the triangulator; evenly spaced points don't.
        parts = [p if _triangulate(p) is not None else p.segmentize(0.5) for p in getattr(pocket, "geoms", [pocket])]
        pieces += [_axis_prism(f, part, *cut_ends) for part in parts]
        e = _edit(f"grip-fins-{f['region']}", "press_fit_fins", f"{count} grip fins in the Ø{f['diameter']:.2f} mm hole (instead of a rigid press fit)",
                  f"Opens the hole to a Ø{2 * r_pocket:.1f} mm pocket with {count} angled fins, {thick:.1f} mm thick, whose tips reach "
                  f"{G['interference_mm']:.1f} mm into the Ø{want['partner_mm']:g} mm part. The fins bend as it goes in and keep "
                  "gripping even when your printer makes holes a tenth or two off, where a rigid press fit goes loose or cracks "
                  "the part. Slice with Arachne walls so each fin prints as one solid loop.", pieces, op="cut")
        e["piece_ops"] = ["add"] * len(fins) + ["cut"] * (len(pieces) - len(fins))
        out.append(e)
    return out


def _ends(f: dict, add: bool) -> tuple[float, float]:
    """Where a ring along a hole starts and ends: a hair inside the hole's end faces when it adds plastic,
    a hair past them when it cuts. Ending exactly on the part's faces leaves broken slivers there."""
    half = f["length"] / 2 + (-OFF_CORNER if add else OFF_CORNER)
    return -half, half


def _ring(f: dict, inner: float, outer: float, add: bool = False) -> np.ndarray:
    """A ring (or disc, inner = 0) along a hole / post's axis, over its length (see ``_ends``)."""
    from shapely.geometry import Point

    from .additions import extrude

    shape = Point(0, 0).buffer(outer, 64)
    if inner > 0:
        shape = shape.difference(Point(0, 0).buffer(inner, 64))
    axis, c = np.asarray(f["axis"]), np.asarray(f["center"])
    u = np.cross(axis, [1.0, 0, 0] if abs(axis[0]) < 0.9 else [0, 1.0, 0])
    u /= np.linalg.norm(u)
    s = np.cross(axis, u)
    local = extrude(shape, *_ends(f, add))
    return c + local[..., :1] * u + local[..., 1:2] * s + local[..., 2:] * axis


def _to_part(mesh: Mesh, r: np.ndarray):
    """Map triangles from the oriented part (orient(mesh, r)) back to part coordinates."""
    pts = mesh.triangles.reshape(-1, 3) @ r.T
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    shift = np.array([-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]])
    return lambda tris: ((np.asarray(tris).reshape(-1, 3) - shift) @ r).reshape(-1, 3, 3)  # r.T @ v per point


TEARDROP_FLAT_MM = 0.5  # the point ends in a flat about one line wide


def _teardrop_cutter(f: dict, up: np.ndarray, angle_deg: float = 45.0) -> np.ndarray:
    """Kite-shaped prism (centre, two tangent points, roof) along the hole axis. The roof
    lines are ``angle_deg`` from vertical: they touch the circle at r·(sin θ up ± cos θ side)
    and would meet at r / sin θ above the centre. They stop ``TEARDROP_FLAT_MM`` apart instead: a
    knife-sharp tip is a hairline slit in the last layer it reaches, narrower than any gap that prints
    open (gap_fuses, T-488), and a flat that short bridges cleanly. The tangent points sit a hair
    outside the circle and the prism a hair past both ends of the hole: touching the hole's own corners
    and end faces exactly leaves zero-thickness fins in the result."""
    axis, c, r = np.asarray(f["axis"]), np.asarray(f["center"]), f["radius"]
    u = up - (up @ axis) * axis
    u /= np.linalg.norm(u)
    s = np.cross(axis, u)
    th = np.radians(angle_deg)
    rt = r + OFF_CORNER
    up_k, side_k = rt * np.sin(th), rt * np.cos(th)
    half = TEARDROP_FLAT_MM / 2
    flat_k = r / np.sin(th) - half / np.tan(th)  # height of the flat above the centre
    profile = [c - 0.5 * r * u, c + side_k * s + up_k * u, c + half * s + flat_k * u,
               c - half * s + flat_k * u, c - side_k * s + up_k * u]
    return _prism(profile, axis * (f["length"] / 2 + 5 * OFF_CORNER))


SKIP_ORDER = ("teeth", "thread", "fit", "fine")
SKIP_LABELS = {
    "teeth": "the teeth",
    "thread": "the threads",
    "fit": "the sized fit",
    "fine": "the fine details",
}


def _skip_note(reasons: list[str]) -> str:
    """Plain-language sentence naming the working shapes left alone, once per kind."""
    ordered = [SKIP_LABELS[k] for k in SKIP_ORDER if k in reasons]
    if not ordered:
        return ""
    what = ordered[0] if len(ordered) == 1 else ", ".join(ordered[:-1]) + f" and {ordered[-1]}"
    return f"Left {what} alone."


def _attach_skip_notes(corner_edits: list[dict], corner_skipped: list[str],
                       foot_edits: list[dict], foot_skipped: list[str]) -> None:
    """Say once on a remaining corner or foot edit which working shapes were kept untouched."""
    if corner_edits and corner_skipped:
        corner_edits[0]["description"] += " " + _skip_note(corner_skipped)
    foot_reasons = list(foot_skipped) + ([] if corner_edits else list(corner_skipped))
    if foot_edits and foot_reasons:
        foot_edits[0]["description"] += " " + _skip_note(foot_reasons)


def _bed_protection(oriented: Mesh, region: np.ndarray, r: np.ndarray,
                    fits: dict | None, gaps: dict | None) -> dict:
    """Working-shape context shared by corner rounding and the bottom chamfer: triangles of sized fits
    (round holes / posts and flat slots / tabs) and modelled threads, both in ``oriented`` coordinates."""
    fit_regions = {int(k) for k in (fits or {})} | {int(k) for pair in (gaps or {}) for k in pair}
    fit_tris = oriented.triangles[np.isin(region, list(fit_regions))] if fit_regions else np.zeros((0, 3, 3))
    threads = find_threads(oriented, axes=r.T)
    return {"fit_tris": fit_tris, "threads": threads}


def _slice_segments(tris: np.ndarray, z: float) -> np.ndarray:
    """2D segments where triangles ``tris`` cross height ``z``, shape (K, 2, 2)."""
    if len(tris) == 0:
        return np.zeros((0, 2, 2))
    a, b = tris, tris[:, [1, 2, 0]]
    da, db = a[..., 2] - z, b[..., 2] - z
    crossing = da * db < 0
    swap = (da > db)[..., None]
    lo, hi = np.where(swap, b, a), np.where(swap, a, b)
    t = (z - lo[..., 2]) / (hi[..., 2] - lo[..., 2] + (~crossing))
    pts = lo[..., :2] + t[..., None] * (hi[..., :2] - lo[..., :2])
    ok = crossing.sum(axis=1) == 2
    return pts[ok][crossing[ok]].reshape(-1, 2, 2) if ok.any() else np.zeros((0, 2, 2))


def _point_to_segments_dist(pts: np.ndarray, segs: np.ndarray) -> np.ndarray:
    """Shortest 2D distance from each point in ``pts`` (M, 2) to any segment in ``segs`` (K, 2, 2)."""
    if len(pts) == 0:
        return np.zeros(0)
    if len(segs) == 0:
        return np.full(len(pts), np.inf)
    a = segs[:, 0]
    ab = segs[:, 1] - a
    len2 = np.maximum((ab * ab).sum(axis=1), 1e-12)
    best = np.full(len(pts), np.inf)
    for k in range(0, len(segs), 200):
        ak, abk, l2k = a[k:k + 200], ab[k:k + 200], len2[k:k + 200]
        t = np.clip(np.einsum("pkc,kc->pk", pts[:, None] - ak[None], abk) / l2k[None], 0.0, 1.0)
        d = np.linalg.norm(pts[:, None] - (ak[None] + t[..., None] * abk[None]), axis=2)
        best = np.minimum(best, d.min(axis=1))
    return best


def _ring_turns(c: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per vertex of closed ring ``c`` (material on the left): signed turn in degrees (+ convex,
    - concave), incoming edge length and outgoing edge length."""
    e1 = c - np.roll(c, 1, axis=0)
    e2 = np.roll(c, -1, axis=0) - c
    l1, l2 = np.linalg.norm(e1, axis=1), np.linalg.norm(e2, axis=1)
    cross = e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]
    dot = np.einsum("ij,ij->i", e1, e2)
    return np.degrees(np.arctan2(cross, dot)), l1, l2


def _fit_circle_2d(pts: np.ndarray) -> tuple[np.ndarray, float] | None:
    """Algebraic least-squares circle fit to 2D points (centred for stability)."""
    if len(pts) < 3:
        return None
    mean = pts.mean(axis=0)
    q = pts - mean
    A = np.column_stack([q, np.ones(len(q))])
    sol, *_ = np.linalg.lstsq(A, -(q * q).sum(axis=1), rcond=None)
    cx, cy = -sol[0] / 2.0, -sol[1] / 2.0
    r2 = cx * cx + cy * cy - sol[2]
    if not np.isfinite(r2) or r2 <= 1e-6:
        return None
    return np.array([cx, cy]) + mean, float(np.sqrt(r2))


def _longest_circular_run(mask: np.ndarray) -> tuple[int, int]:
    """(start index, length) of the longest contiguous True run in circular boolean ``mask``."""
    n = len(mask)
    if n == 0 or not mask.any():
        return 0, 0
    if mask.all():
        return 0, n
    doubled = np.concatenate([mask, mask])
    padded = np.r_[False, doubled, False]
    changes = np.flatnonzero(padded[1:] != padded[:-1])
    starts, ends = changes[0::2], changes[1::2]
    lengths = np.minimum(ends - starts, n)
    best = int(np.argmax(lengths))
    return int(starts[best] % n), int(lengths[best])


def _radial_candidates(c: np.ndarray, idx: np.ndarray, turn: np.ndarray,
                       min_repeats: int, turn_tol: float) -> list[tuple[np.ndarray, float]]:
    """Candidate (centre, radius) circle fits for corners ``idx``: whole set first (full gears /
    knurls), then longest repeating chains along the ring (sector gears or gears with a tab)."""
    out = []
    fit = _fit_circle_2d(c[idx])
    if fit is not None:
        out.append(fit)
    pts = c[idx]
    t = turn[idx]
    for period in (1, 2):
        if len(idx) < period * min_repeats:
            continue
        step_d = np.linalg.norm(np.roll(pts, -period, axis=0) - pts, axis=1)
        med = float(np.median(step_d))
        if med <= 1e-6:
            continue
        same = (np.abs(t - np.roll(t, -period)) <= turn_tol) & (np.abs(step_d - np.roll(step_d, -period)) <= 0.25 * med + 0.1)
        start, run_len = _longest_circular_run(same)
        if run_len >= period * (min_repeats - 1):
            chain = idx[(start + np.arange(min(run_len + period, len(idx)))) % len(idx)]
            fit = _fit_circle_2d(c[chain])
            if fit is not None:
                out.append(fit)
    return out


def _radial_pattern_vertices(c: np.ndarray, signed_turn: np.ndarray, W: dict) -> np.ndarray:
    """Vertices on ring ``c`` belonging to a repeating radial pattern around a centre (gear teeth,
    splines, knurls): many equal corners at equal angles with both outward and inward bends (so
    squares, hexagons, octagons and smooth circles are never caught)."""
    n = len(c)
    none = np.zeros(n, dtype=bool)
    min_rep = int(W["radial_min_repeats"])
    min_turn = float(W["radial_min_turn_deg"])
    # Teeth / flutes bend both ways; convex shapes (circles, hexagons, boxes) bend only one way.
    if signed_turn[signed_turn > 1.0].sum() < min_rep * min_turn or (-signed_turn[signed_turn < -1.0]).sum() < min_rep * min_turn:
        return none
    turn = np.abs(signed_turn)
    turn_tol = float(W["radial_turn_tol_deg"])
    ang_tol = float(W["radial_angle_tol_share"])
    min_depth = float(W["radial_min_depth_mm"])
    max_step = 2 * np.pi / min_rep * (1.0 + ang_tol)

    for idx in (np.flatnonzero(signed_turn >= min_turn), np.flatnonzero(signed_turn <= -min_turn)):
        if len(idx) < min_rep:
            continue
        for centre, radius in _radial_candidates(c, idx, turn, min_rep, turn_tol):
            r_tol = max(float(W["radial_radius_tol_share"]) * radius, float(W["radial_radius_tol_mm"]))
            r_idx = np.linalg.norm(c[idx] - centre, axis=1)
            sub = idx[np.abs(r_idx - radius) <= r_tol]
            if len(sub) < min_rep:
                continue
            th = np.arctan2(c[sub, 1] - centre[1], c[sub, 0] - centre[0]) % (2 * np.pi)
            order = np.argsort(th)
            sub, th = sub[order], th[order]
            t_sub = turn[sub]
            r_all = np.linalg.norm(c - centre, axis=1)
            for period in (1, 2):
                if len(sub) < period * min_rep:
                    continue
                d_th = (np.roll(th, -period) - th) % (2 * np.pi)
                small = d_th[(d_th > 1e-3) & (d_th <= max_step)]
                if len(small) < period * (min_rep - 1):
                    continue
                step = float(np.median(small))
                ok = (np.abs(d_th - step) <= ang_tol * step) & (np.abs(t_sub - np.roll(t_sub, -period)) <= turn_tol)
                start, run_len = _longest_circular_run(ok)
                if run_len < period * (min_rep - 1):
                    continue
                if run_len == len(sub):
                    if r_all.max() - r_all.min() >= min_depth:
                        return np.ones(n, dtype=bool)
                    continue
                matched = np.sort(sub[(start + np.arange(run_len + period)) % len(sub)])
                marked = np.zeros(n, dtype=bool)
                angs = np.arctan2(c[:, 1] - centre[1], c[:, 0] - centre[0])
                for a, b in zip(matched, np.roll(matched, -1)):
                    da = abs((angs[b] - angs[a] + np.pi) % (2 * np.pi) - np.pi)
                    if da <= 1.5 * step:
                        span = (int(b) - int(a)) % n
                        marked[(int(a) + np.arange(span + 1)) % n] = True
                # Include the end tooth flanks down to their adjacent root vertices.
                ends = np.flatnonzero(marked & (~np.roll(marked, 1) | ~np.roll(marked, -1)))
                for k in ends:
                    for nbr in ((k - 1) % n, (k + 1) % n):
                        if not marked[nbr] and abs(r_all[nbr] - radius) >= min_depth:
                            if abs((angs[nbr] - angs[k] + np.pi) % (2 * np.pi) - np.pi) <= 0.75 * step:
                                marked[nbr] = True
                if marked.any() and r_all[marked].max() - r_all[marked].min() >= min_depth:
                    return marked
    return none


def _close_corner_mask(c: np.ndarray, signed_turn: np.ndarray, line_width: float, W: dict,
                       ignore: np.ndarray | None = None) -> np.ndarray:
    """Sharp corners on ring ``c`` that have another sharp corner within a few printed line widths
    (snap hooks, latches, clips, fine slots rather than large bed corners that warp)."""
    is_sharp = np.abs(signed_turn) >= float(W["close_min_turn_deg"])
    if ignore is not None:
        is_sharp &= ~ignore
    sharp = np.flatnonzero(is_sharp)
    mask = np.zeros(len(c), dtype=bool)
    if len(sharp) < 2:
        return mask
    limit = float(W["close_corners_lines"]) * line_width
    pts = c[sharp]
    close = np.zeros(len(pts), dtype=bool)
    for k in range(0, len(pts), 200):
        chunk = pts[k:k + 200]
        d = np.linalg.norm(chunk[:, None] - pts[None, :], axis=2)
        d[np.arange(len(chunk)), np.arange(k, k + len(chunk))] = np.inf
        close[k:k + len(chunk)] = d.min(axis=1) < limit
    mask[sharp[close]] = True
    return mask


def _on_threads(pts: np.ndarray, z0: float, threads: list[dict], W: dict) -> np.ndarray:
    """True for 2D points on layer ``z0`` that sit on a detected thread reaching the bed."""
    mask = np.zeros(len(pts), dtype=bool)
    if not threads or len(pts) == 0:
        return mask
    base_tol = float(W["thread_tol_mm"])
    p3 = np.column_stack([pts, np.full(len(pts), z0)])
    for t in threads:
        tol = base_tol + 0.5 * float(t["pitch_mm"])
        if t["lo"][2] > z0 + tol:
            continue
        centre = np.asarray(t["center"], float)
        axis = np.asarray(t["axis"], float)
        v = p3 - centre
        along = v @ axis
        radial = np.linalg.norm(v - np.outer(along, axis), axis=1)
        in_len = np.abs(along) <= float(t["length_mm"]) / 2 + tol
        r_max = float(t["major_mm"]) / 2 + tol
        if abs(axis[2]) > 0.7:  # upright thread: annulus so an outer block around a threaded hole stays free
            r_min = max(float(t["minor_mm"]) / 2 - tol, 0.0)
            mask |= in_len & (radial >= r_min) & (radial <= r_max)
        else:  # thread lying across the bed: bed slice cuts across the chord of the cylinder
            mask |= in_len & (radial <= r_max)
    return mask


def _classify_ring(c: np.ndarray, z0: float, line_width: float,
                   fit_segs: np.ndarray, threads: list[dict], W: dict) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
    """Per-vertex and per-edge working-shape masks on simplified ring ``c`` (material on left).
    Priority: thread > fit > teeth > fine, so a small gear reports teeth rather than fine details."""
    signed_turn, _, l2 = _ring_turns(c)
    mids = 0.5 * (c + np.roll(c, -1, axis=0))
    fit_tol = float(W["fit_tol_mm"])
    close_limit = float(W["close_corners_lines"]) * line_width

    v_thread = _on_threads(c, z0, threads, W)
    e_thread = _on_threads(mids, z0, threads, W) | (v_thread & np.roll(v_thread, -1))

    v_fit = (_point_to_segments_dist(c, fit_segs) <= fit_tol) & ~v_thread
    e_fit = (_point_to_segments_dist(mids, fit_segs) <= fit_tol) & (v_fit | v_thread) & (np.roll(v_fit | v_thread, -1)) & ~e_thread

    v_teeth = _radial_pattern_vertices(c, signed_turn, W) & ~(v_thread | v_fit)
    e_teeth = (v_teeth & np.roll(v_teeth, -1)) & ~(e_thread | e_fit)

    v_fine = _close_corner_mask(c, signed_turn, line_width, W, ignore=v_thread | v_fit | v_teeth)
    v_fine_nxt = np.roll(v_fine, -1)
    e_fine = ((v_fine & v_fine_nxt) | ((v_fine | v_fine_nxt) & (l2 < close_limit))) & ~(e_thread | e_fit | e_teeth)

    return ({"thread": v_thread, "fit": v_fit, "teeth": v_teeth, "fine": v_fine},
            {"thread": e_thread, "fit": e_fit, "teeth": e_teeth, "fine": e_fine})


def _corner_chamfers(oriented: Mesh, to_part, profile, size: float = 3.0,
                     protect: dict | None = None, skipped: list[str] | None = None) -> list[dict]:
    """Round sharp footprint corners (where warping starts): a vertical fillet of radius ``size``,
    cut as a fan of thin wedges between the corner and the arc. Offered for warp-prone materials,
    and for others once the footprint is a fair part of the length at which they start to lift.
    Skips working shapes (teeth/splines/knurls, threads, sized fits, closely spaced corners)."""
    from shapely.geometry.polygon import orient as orient_poly

    from .checks import _rect_sides, _sharp_corners
    from .slicer import parts

    layers = slice_mesh(oriented, profile.layer_height, max_layers=60)
    first = layers[0].geom
    if first.is_empty:
        return []
    corners = _sharp_corners(first)
    if not corners:
        return []
    thresholds = load_data("thresholds")
    if profile.material["warp_risk"] == "low":
        risk = thresholds["warping"]["risk_length_mm"]
        limit = risk.get(profile.material.get("based_on", profile.material_id), {}).get(profile.enclosure)
        if not limit or _rect_sides(first.minimum_rotated_rectangle)[1] < CORNER_ROUND_SHARE * limit:
            return []
    W = thresholds["working_shapes"]
    z0 = layers[0].z
    fit_segs = _slice_segments((protect or {}).get("fit_tris", np.zeros((0, 3, 3))), z0)
    threads = (protect or {}).get("threads", [])
    classified = []
    for poly in parts(first):
        c = np.asarray(orient_poly(poly.simplify(0.05), 1.0).exterior.coords)[:-1, :2]
        c = c[np.linalg.norm(c - np.roll(c, 1, axis=0), axis=1) > 1e-3]
        if len(c) >= 3:
            v_masks, _ = _classify_ring(c, z0, profile.line_width, fit_segs, threads, W)
            classified.append((c, v_masks))
    out = []
    for x, y in corners:
        p, reason = _match_corner(np.array([x, y]), classified)
        if reason is not None:
            if skipped is not None and reason not in skipped:
                skipped.append(reason)
            continue
        top = _corner_height(layers, p)
        e1, e2, edge = _corner_edges(first, p)
        if e1 is None:
            continue
        half_angle = np.arccos(np.clip(e1 @ e2, -1, 1)) / 2
        # Two roundings on a short edge must not meet, or they leave a new point between them.
        r = min(size, 0.4 * edge * np.tan(half_angle))
        reach = r / np.tan(half_angle)  # corner to where the arc meets each edge
        bis = (e1 + e2) / np.linalg.norm(e1 + e2)
        # A hair deeper than tangent: an arc that just touches the walls leaves zero-thickness fins there.
        centre = p + bis * (r / np.sin(half_angle) + 3 * OFF_CORNER)
        s1, s2 = p + e1 * reach - centre, p + e2 * reach - centre
        a1, a2 = np.arctan2(s1[1], s1[0]), np.arctan2(s2[1], s2[0])
        sweep = (a2 - a1 + np.pi) % (2 * np.pi) - np.pi
        arc = [centre + r * np.array([np.cos(a), np.sin(a)]) for a in a1 + sweep * np.linspace(0, 1, CORNER_ARC_STEPS + 1)]
        tip = p - bis * 0.5  # a little past the corner, so the cut leaves no skin
        mid = np.array([0, 0, top / 2 - 0.25])
        half = np.array([0, 0, top / 2 + 0.25])
        piece = _fan_prism([tip, *arc], mid[2] - half[2], mid[2] + half[2])  # one piece: no faces shared between pieces
        n_idx = len(out)
        out.append(_edit(
            f"chamfer-{n_idx}", "sharp_bed_corners", f"Round sharp corner {n_idx + 1} (r {r:.1f} mm)",
            "A rounded corner shrinks more evenly and is much less likely to lift off the bed (warping starts at sharp corners).",
            [to_part(piece)], param="chamfer_mm"))
    return out


def _match_corner(p: np.ndarray, classified: list[tuple[np.ndarray, dict[str, np.ndarray]]]) -> tuple[np.ndarray, str | None]:
    """Match rounded corner ``p`` (from _sharp_corners) back to its exact vertex on the simplified
    exterior ring and return ``(exact_point, skip_reason)`` (reason is None to keep)."""
    for c, v_masks in classified:
        d = np.linalg.norm(c - p, axis=1)
        k = int(np.argmin(d))
        if d[k] < 0.02:
            for reason in SKIP_ORDER:
                if v_masks[reason][k]:
                    return c[k], reason
            return c[k], None
    return p, None


def _foot_chamfer(oriented: Mesh, to_part, profile, size: float,
                  protect: dict | None = None, skipped: list[str] | None = None) -> list[dict]:
    """A small 45° chamfer along every bottom edge (outline and holes): the first layers are squashed
    and bulge out (elephant foot); the chamfer takes that material away, so edges stay straight and
    holes at the bed keep their size. Working outlines (teeth/splines/knurls, threads, sized fits,
    closely spaced fine details) are skipped so the chamfer never ruins them.

    Each outline loop is cut by ONE closed piece: the chamfer's cross-section swept along the loop,
    mitred at the corners. The sections sit just before and after each corner, never on it: a
    section through the part's own corner edge lets the two meshes touch edge on edge, and the
    boolean leaves zero-thickness fins and crumbs there (mesh errors in the slicer). Sharp corners
    break the sweep: there the pieces end square, reaching past outside corners and joined by a
    filler piece at inside corners."""
    from shapely.geometry.polygon import orient as orient_poly

    from .slicer import parts

    layers = slice_mesh(oriented, profile.layer_height, max_layers=2)
    first = layers[0].geom
    if first.is_empty:
        return []
    W = load_data("thresholds")["working_shapes"]
    z0 = layers[0].z
    fit_segs = _slice_segments((protect or {}).get("fit_tris", np.zeros((0, 3, 3))), z0)
    threads = (protect or {}).get("threads", [])
    min_stretch = float(W["min_foot_stretch_mm"])
    # Cross-section (outward offset, height): the 45° line from (-size, 0) to (0, size), extended past both ends.
    section = np.array([[-(size + 0.3), -0.3], [0.3, size + 0.3], [0.3, -0.3]])
    pieces, points = [], 0
    for poly in parts(first):
        poly = orient_poly(poly.simplify(0.05), 1.0)  # exterior CCW, holes CW: material on the left
        for ring in [poly.exterior, *poly.interiors]:
            c = np.asarray(ring.coords)[:-1, :2]
            c = c[np.linalg.norm(c - np.roll(c, 1, axis=0), axis=1) > 1e-3]
            if len(c) < 3:
                continue
            _, e_masks = _classify_ring(c, z0, profile.line_width, fit_segs, threads, W)
            skip_edges = np.zeros(len(c), dtype=bool)
            for reason in SKIP_ORDER:
                if e_masks[reason].any():
                    skip_edges |= e_masks[reason]
                    if skipped is not None and reason not in skipped:
                        skipped.append(reason)
            if skip_edges.all():
                continue
            ring_pieces = _ring_chamfer(c, section, size, skip_edges if skip_edges.any() else None, min_stretch)
            points += int((~skip_edges).sum())
            if points > MAX_FOOT_POINTS:
                return []  # a very detailed footprint: leave it to the slicer's elephant foot compensation
            pieces += ring_pieces
    if not pieces:
        return []
    return [_edit("foot-chamfer", "elephant_foot", f"{size:.1f} mm chamfer along the bottom edge",
                  "The first layers are pressed flat and bulge out (elephant foot). A small 45° chamfer at the bottom edge "
                  "takes that away, so the edges look straight and holes at the bed keep their size.",
                  [to_part(t) for t in pieces], param="foot_chamfer_mm")]


SHARP_MITRE = 0.5  # cos of half the turn: corners turning more than 120° break the sweep


def _ring_chamfer(c: np.ndarray, section: np.ndarray, size: float,
                  skip_edges: np.ndarray | None = None, min_stretch: float = 3.0) -> list[np.ndarray]:
    """Closed pieces cutting the chamfer along one outline loop ``c`` (material on the left). None
    when a piece would fold (a short edge after a big turn) or run into itself (a spot narrower than
    the cutter, which reaches in from both sides): then this loop keeps its plain edge.
    ``skip_edges`` (bool per edge): working-shape stretches to leave un-chamfered; open sweeps stop
    cleanly short of them and drop stubs shorter than ``min_stretch``."""
    n = len(c)
    edge_vec = np.roll(c, -1, axis=0) - c
    edge_len = np.linalg.norm(edge_vec, axis=1)
    d = edge_vec / edge_len[:, None]  # edge k runs from c[k] to c[k + 1]
    out = np.stack([d[:, 1], -d[:, 0]], axis=1)  # right of the edge = outside
    d_in, o_in = np.roll(d, 1, axis=0), np.roll(out, 1, axis=0)  # the edge arriving at each vertex
    mitre = o_in + out
    mitre /= np.maximum(np.linalg.norm(mitre, axis=1, keepdims=True), 1e-12)
    cos_half = np.einsum("ij,ij->i", mitre, out)
    convex = d_in[:, 0] * d[:, 1] - d_in[:, 1] * d[:, 0] > 0
    sharp = np.flatnonzero(cos_half < SHARP_MITRE).tolist()

    def at(p, o):  # the cross-section at point p, offsets along o
        return np.array([[*(p + o * s), z] for s, z in section])

    def corner(k):  # mitred sections just before and after corner k, each on its own edge's offset lines
        o = mitre[k] / cos_half[k]
        return [at(c[k] - d_in[k] * OFF_CORNER, o), at(c[k] + d[k] * OFF_CORNER, o)]

    def forward(rings, dirs):  # every section point moves on along its edge: the piece doesn't fold
        return all(np.all((b - a)[:, :2] @ e > 1e-6) for a, b, e in zip(rings, rings[1:], dirs))

    def apart(rings, closed):  # the cutter's inner and outer edges don't cross themselves
        loop = rings + rings[:1] if closed else rings
        return all(LineString([r[i, :2] for r in loop]).is_simple for i in (0, 2))

    if skip_edges is not None and skip_edges.any():
        if skip_edges.all():
            return []
        s0 = int(np.flatnonzero(skip_edges)[0])
        runs, cur = [], []
        for i in range(1, n):
            k = (s0 + i) % n
            if not skip_edges[k]:
                cur.append(k)
            elif cur:
                runs.append(cur)
                cur = []
        if cur:
            runs.append(cur)
        trim = size + 0.3
        pieces = []
        for run in runs:
            cuts = [0] + [j for j in range(1, len(run)) if cos_half[run[j]] < SHARP_MITRE] + [len(run)]
            for j0, j1 in zip(cuts, cuts[1:]):
                ks = run[j0:j1]
                start_skipped = bool(skip_edges[(ks[0] - 1) % n])
                end_skipped = bool(skip_edges[(ks[-1] + 1) % n])
                while len(ks) > 1 and start_skipped and edge_len[ks[0]] <= trim + 2 * OFF_CORNER:
                    ks = ks[1:]
                while len(ks) > 1 and end_skipped and edge_len[ks[-1]] <= trim + 2 * OFF_CORNER:
                    ks = ks[:-1]
                usable = float(edge_len[ks].sum()) - (trim if start_skipped else 0.0) - (trim if end_skipped else 0.0)
                if usable < min_stretch:
                    continue
                a, b = ks[0], (ks[-1] + 1) % n
                start = c[a] + d[a] * trim if start_skipped else c[a] - d[a] * (trim if convex[a] else -OFF_CORNER)
                end = c[b] - d[ks[-1]] * trim if end_skipped else c[b] + d[ks[-1]] * (trim if convex[b] else -OFF_CORNER)
                rings = [at(start, out[a])] + [r for k in ks[1:] for r in corner(k)] + [at(end, out[ks[-1]])]
                if not (forward(rings, [d[a]] + [d[k] for k in ks[1:] for _ in range(2)]) and apart(rings, closed=False)):
                    continue
                pieces.append(_sweep(rings, closed=False))
                if not end_skipped and not convex[b]:
                    back, ahead = at(c[b] - d[ks[-1]] * 0.05, out[ks[-1]]), at(c[b] + d[b] * 0.05, out[b])
                    pieces.append(_sweep([back, ahead], closed=False))
        return pieces

    if not sharp:
        rings = [r for k in range(n) for r in corner(k)]
        ok = forward(rings + rings[:1], [d[i // 2] for i in range(2 * n)]) and apart(rings, closed=True)
        return [_sweep(rings, closed=True)] if ok else []
    pieces = []
    for a, b in zip(sharp, sharp[1:] + sharp[:1]):
        ks = [(a + i) % n for i in range((b - a) % n or n)]  # edges a … b-1, from sharp corner a to sharp corner b
        # Square ends: past outside corners (so the chamfer runs out cleanly), short of inside ones.
        start = c[a] - d[a] * ((size + 0.3) if convex[a] else -OFF_CORNER)
        end = c[b] + d[ks[-1]] * ((size + 0.3) if convex[b] else -OFF_CORNER)
        rings = [at(start, out[a])] + [r for k in ks[1:] for r in corner(k)] + [at(end, out[ks[-1]])]
        if not (forward(rings, [d[a]] + [d[k] for k in ks[1:] for _ in range(2)]) and apart(rings, closed=False)):
            return []
        pieces.append(_sweep(rings, closed=False))
        if not convex[b]:  # inside corner: fill the gap between the two square ends
            back, ahead = at(c[b] - d[ks[-1]] * 0.05, out[ks[-1]]), at(c[b] + d[b] * 0.05, out[b])
            pieces.append(_sweep([back, ahead], closed=False))
    return pieces


def _fan_prism(pts: list[np.ndarray], z0: float, z1: float) -> np.ndarray:
    """Closed prism from z0 to z1 over a polygon that every point sees from its first one (a fan)."""
    lo = [np.array([*q, z0]) for q in pts]
    hi = [np.array([*q, z1]) for q in pts]
    n = len(pts)
    tris = [t for i in range(1, n - 1) for t in ([lo[0], lo[i + 1], lo[i]], [hi[0], hi[i], hi[i + 1]])]
    for i in range(n):
        j = (i + 1) % n
        tris += [[lo[i], lo[j], hi[j]], [lo[i], hi[j], hi[i]]]
    t = np.array(tris)
    return t[:, ::-1] if Mesh(t).volume() < 0 else t


def _sweep(rings: list[np.ndarray], closed: bool) -> np.ndarray:
    """Closed mesh through matching cross-sections (each (m, 3), convex), capped unless ``closed``."""
    m = len(rings[0])
    tris = []
    for a, b in zip(rings, rings[1:] + rings[:1] if closed else rings[1:]):
        for i in range(m):
            j = (i + 1) % m
            tris += [[a[i], a[j], b[j]], [a[i], b[j], b[i]]]
    if not closed:
        for i in range(1, m - 1):
            tris += [[rings[0][0], rings[0][i + 1], rings[0][i]], [rings[-1][0], rings[-1][i], rings[-1][i + 1]]]
    t = np.array(tris)
    return t[:, ::-1] if Mesh(t).volume() < 0 else t


def _stress_chamfers(mesh: Mesh, weak_spot, h: float, size: float, hot_spots=None, up=None,
                     max_overhang_deg: float = 45.0) -> list[dict]:
    """Rounds (fillets) along the inside corner nearest the weak spot from the strength check (e.g. where
    a stem meets a plate): sharp inside corners concentrate stress, a round spreads it out best. Where a
    round would hang over (a corner under a ledge: its surface faces down) it is a 45° chamfer instead,
    which supports itself (rules.yaml stress_at_inside_corner; ``up`` is the print direction). Takes the
    whole connected corner (all inside edges linked to the ones near the weak spot), and the other
    inside corners the stress is nearly as high at (``hot_spots``): with only the worst one, the weak
    spot moves to the next corner and the user needs a strength check per corner."""
    if weak_spot is None:
        return []
    edges = concave_edges(mesh)
    if not edges:
        return []
    spot = np.asarray(weak_spot, float)
    mids = np.array([(e["a"] + e["b"]) / 2 for e in edges])
    dist = np.linalg.norm(mids - spot, axis=1)
    if dist.min() > max(CHAMFER_REACH_VOXELS * h, size * 2):
        return []  # the weak spot isn't at an inside corner
    corners = [_connected(edges, [int(np.argmin(dist))])]
    if hot_spots is not None:
        heat = _edge_heat(edges, *hot_spots, HOT_REACH_VOXELS * h)
        left = set(range(len(edges))) - set(corners[0])
        others = []
        while left:
            corner = _connected(edges, [min(left)])
            left -= set(corner)
            if heat[corner].max() > 0:
                others.append((heat[corner].max(), corner))
        corners += [c for _, c in sorted(others, key=lambda o: -o[0])[:MAX_STRESS_CORNERS - 1]]
    pieces, rounds, profiles = [], 0, {}
    for k in [k for c in corners for k in c][:MAX_FILLET_PIECES]:
        e = edges[k]
        mid = (e["a"] + e["b"]) / 2
        half = (e["b"] - e["a"]) / 2
        inset = -(e["n1"] + e["n2"]) * 0.15  # reach a little into the part, so the union joins it
        rounded = up is None or _round_prints(e, up, max_overhang_deg)
        d = size * ROUND_SAME_PLASTIC if rounded else size
        ends = [mid + e["t1"] * d - e["n1"] * 0.15, mid + e["t2"] * d - e["n2"] * 0.15]
        profile = [mid + inset, ends[0], *_round_arc(e, mid, d), ends[1]] if rounded else [mid + inset, *ends]
        pieces.append(_prism(profile, half))
        rounds += rounded
        profiles[k] = (profile, mid)
    pieces += _corner_pieces(edges, profiles)
    r_mm = size * ROUND_SAME_PLASTIC
    every = len(profiles)  # edges; the corner pieces follow their edges
    shape = (f"{r_mm:.1f} mm round" if rounds == every else f"{size:.1f} mm 45° chamfer" if not rounds
             else f"{r_mm:.1f} mm round / {size:.1f} mm chamfer")
    shape = shape[0].upper() + shape[1:]
    if len(corners) == 1:
        title = f"{shape} where the part is weakest"
        found = "The strength check found the most stress at this sharp inside corner. "
    else:
        title = f"{shape} at the {len(corners)} inside corners with the most stress"
        found = ("The strength check found the most stress at these sharp inside corners, each nearly as weak as the "
                 "worst one: fixing only one would just move the weak spot to the next. ")
    how = ("A round spreads the load over more material, often the cheapest way to make a part much stronger."
           if rounds == every else
           "A 45° chamfer spreads the load over more material and prints without support where a round would hang over."
           if not rounds else
           "Rounds spread the load best; where one would hang over in this orientation it is a 45° chamfer, which prints "
           "without support.")
    return [_edit("stress-chamfer", "stress_at_inside_corner", title, found + how, pieces, op="add", param="stress_chamfer_mm")]


CORNER_STEPS = 6  # pieces of a corner piece's turn


def _corner_pieces(edges: list[dict], profiles: dict) -> list[np.ndarray]:
    """Fill the gap where two rounded / chamfered inside edges meet at an outside corner (the corner of a
    stem standing on a plate): each edge's piece is straight and ends at the corner, so a wedge was left
    open there (T-358). The first edge's cross-section is swept round the corner, about the line through
    it square to the face both edges share, from one wall's direction to the other's. Where the edges meet
    at an inside corner their pieces already overlap. ``profiles``: {edge index: (cross-section, its middle)}."""
    by_vertex: dict = {}
    for k in profiles:
        for v in (edges[k]["va"], edges[k]["vb"]):
            by_vertex.setdefault(v, []).append(k)
    out = []
    for v, ks in by_vertex.items():
        if len(ks) != 2:
            continue
        e1, e2 = edges[ks[0]], edges[ks[1]]
        shared = next(((i, j) for i in ("n1", "n2") for j in ("n1", "n2") if e1[i] @ e2[j] > 0.99), None)
        if shared is None:
            continue  # no face in common (the corner turns another way): leave it
        axis = e1[shared[0]]
        walls = []
        for e, i in ((e1, shared[0]), (e2, shared[1])):
            w = e["n2" if i == "n1" else "n1"]
            w = w - axis * (w @ axis)
            walls.append(w / max(np.linalg.norm(w), 1e-12))
        r1, r2 = walls
        turn = float(np.arccos(np.clip(r1 @ r2, -1.0, 1.0)))
        corner, other = (e1["a"], e1["b"]) if e1["va"] == v else (e1["b"], e1["a"])
        if turn < np.radians(5) or (other - corner) @ r2 > -1e-6:
            continue  # straight on, or an inside corner
        q = r2 - r1 * (r1 @ r2)
        q /= np.linalg.norm(q)
        profile, mid = profiles[ks[0]]
        flat = [((pt - mid) @ r1, (pt - mid) @ axis) for pt in profile]
        rings = [np.array([corner + s * (np.cos(a) * r1 + np.sin(a) * q) + z * axis for s, z in flat])
                 for a in np.linspace(0.0, turn, CORNER_STEPS + 1)]
        out.append(_sweep(rings, closed=False))
    return out


def _round_prints(e: dict, up, max_overhang_deg: float) -> bool:
    """Whether a round in inside corner ``e`` prints without support: its surface turns from one face's
    direction to the other's, and nowhere in between may it face down more than the material can
    overhang. A round under a ledge starts almost flat and facing down; one that opens upwards (a stem
    on a plate) or stands upright never does."""
    up = np.asarray(up, float) / np.linalg.norm(up)
    limit = np.sin(np.radians(max_overhang_deg))
    for k in (0.25, 0.5, 0.75):
        n = (1 - k) * e["n1"] + k * e["n2"]
        n /= max(np.linalg.norm(n), 1e-12)
        if -(n @ up) > limit:
            return False
    return True


def _round_arc(e: dict, mid: np.ndarray, size: float, steps: int = 6) -> list[np.ndarray]:
    """Inner points of a round filling inside corner ``e``, tangent to both faces ``size`` from the edge
    (the same footprint as a chamfer of that size), from face 1 to face 2."""
    t1, t2 = e["t1"], e["t2"]
    phi = np.arccos(np.clip(t1 @ t2, -1.0, 1.0))  # the open angle between the two faces
    if phi < 1e-3 or phi > np.pi - 1e-3:
        return []
    radius = size * np.tan(phi / 2)
    bis = (t1 + t2) / np.linalg.norm(t1 + t2)
    centre = mid + bis * radius / np.sin(phi / 2)
    a, b = mid + t1 * size - centre, mid + t2 * size - centre
    with np.errstate(invalid="ignore", divide="ignore"):
        turn = np.arccos(np.clip(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)), -1.0, 1.0))
    if not np.isfinite(turn) or turn < 1e-3:  # facets of an earlier round meet almost flat: nothing to round
        return []
    out = []
    for i in range(1, steps):
        k = i / steps
        out.append(centre + (np.sin((1 - k) * turn) * a + np.sin(k * turn) * b) / np.sin(turn))
    return out


def _edge_heat(edges: list[dict], points: np.ndarray, share: np.ndarray, reach: float) -> np.ndarray:
    """Per inside edge: the highest stress share among ``points`` within ``reach`` of it (0 if none).
    Distances to the segment, not its middle: a CAD edge can be one long segment."""
    a = np.array([e["a"] for e in edges])
    ab = np.array([e["b"] for e in edges]) - a
    length2 = np.maximum((ab * ab).sum(axis=1), 1e-12)
    heat = np.zeros(len(edges))
    for k in range(0, len(points), 200):  # in chunks: edges x points can be millions
        p, s = points[k:k + 200], share[k:k + 200]
        t = np.clip(np.einsum("pec,ec->pe", p[:, None] - a[None], ab) / length2, 0, 1)
        d = np.linalg.norm(p[:, None] - (a[None] + t[..., None] * ab[None]), axis=2)
        heat = np.maximum(heat, np.where(d <= reach, s[:, None], 0).max(axis=0))
    return heat


THICKEN_REACH_MM = 8.0  # the added plastic keeps its full thickness at least this far past the stressed stretch
# (and at least the member's own thickness), then ends in a 45° chamfer (T-357: a straight sleeve, not a
# wedge). The end must sit where the stress has dropped: a step right at the weak spot takes the stress
# instead (on a 12 mm bar, an 8 mm taper from the spot made it weaker).
THICKEN_STEP_MM = 0.5  # slab height of the added plastic
NARROW_SHARE = 1.5  # in the weak stretch, sections up to this × the narrowest one get thicker; bigger ones are the wide ends


def _thicker_weak_spot(mesh: Mesh, weak_spot, grow: float, hot_spots=None, up=None) -> list[dict]:
    """Grow the part's cross-section by ``grow`` mm where it is weak: an even sleeve that runs past the
    stressed stretch and ends in a 45° chamfer (see THICKEN_REACH_MM), so no new sharp step takes the
    stress instead. The cross-sections are cut
    across the member the spot is on (its long direction nearby), so an arm gets thicker, not longer.

    Only where it is needed: the full thickness runs along the stretch of the member that is nearly as
    stressed as the weak spot (``hot_spots`` from the strength check), the sleeve past it continues that
    stretch's narrowest shape, and a section that is already bigger than the thickened one (the wide end of
    a dog-bone) gets nothing. With ``up``, nothing grows below the bed (the part must stay flat on it)."""
    if weak_spot is None:
        return []
    from shapely.geometry import Point

    from .additions import extrude
    from .slicer import parts

    spot = np.asarray(weak_spot, float)
    axis, thickness, joined = _weak_axis(mesh, spot, hot_spots)
    r = rotation_to_z(axis)
    turned = mesh.triangles @ r.T
    base = turned[..., 2].min()
    s = r @ spot - [0, 0, base]
    top = turned[..., 2].max() - base
    lo, hi = _weak_stretch(s, r, base, thickness + grow, hot_spots, joined=joined)
    layers = slice_mesh(Mesh(turned - [0, 0, base]), THICKEN_STEP_MM, max_layers=4000)
    bed = _above_bed(turned - [0, 0, base], r, up)

    def section(layer, within: float):
        near = [p for p in parts(layer.geom, 0.05) if p.distance(Point(s[:2])) <= within] if not layer.geom.is_empty else []
        return min(near, key=lambda p: p.distance(Point(s[:2]))) if near else None

    # The narrowest section in and next to the weak stretch is what gets thicker (a neck, not the wide ends
    # it joins): the stretch can lie just inside the wide part, where a stem starts (as in _narrowest).
    wide = 2 * max(thickness, THICKEN_REACH_MM) + grow
    stretch = [p for layer in layers if lo - THICKEN_REACH_MM <= layer.z <= hi + THICKEN_REACH_MM
               for p in [section(layer, wide)] if p is not None]
    narrow = min(stretch, key=lambda p: p.area) if stretch else None
    if narrow is not None:
        x0, y0, x1, y1 = narrow.bounds
        thickness = min(x1 - x0, y1 - y0)
    sleeve = max(THICKEN_REACH_MM, thickness)  # full thickness this far past the stretch...
    reach = sleeve + grow  # ...then a 45° end
    around = Point(s[:2]).buffer(reach + grow, quad_segs=8)
    pieces = []
    for layer in layers:
        dz = max(lo - layer.z, layer.z - hi, 0.0)
        g = grow - max(dz - sleeve, 0.0)
        if dz > reach or g < 0.1 or layer.geom.is_empty:
            continue
        piece = section(layer, max(reach, wide))
        if piece is None:
            continue
        # Sections that match the narrowest one (to 0.1 mm) use it, so the sleeve's slabs are identical and
        # join without hair-thin steps (each slice differs a little; the steps were taken for inside corners).
        own = dz == 0 and (narrow is None or (piece.area <= NARROW_SHARE * narrow.area and not _same_outline(piece, narrow)))
        shape = piece if own or narrow is None else narrow
        x0, y0, x1, y1 = shape.bounds
        grown = shape.buffer(g, quad_segs=4)
        if not own and piece.area >= grown.area:
            continue  # already bigger than the thickened neck (a wide end): nothing to add or soften here
        if max(x1 - x0, y1 - y0) > 2 * reach:  # a wide section (a plate): only round the spot
            grown = grown.intersection(around)
        if bed is not None:
            grown = shapely.intersection(grown, bed(layer.z), grid_size=1e-4)
        for poly in parts(grown.simplify(0.05), 0.05):
            if not poly.intersects(piece):  # a bit cut off by the round around the spot: it would float
                continue
            # Slabs reach a whole step into their neighbours, so the sleeve always joins what is next to it:
            # the next layer may lie inside a wide end and get no slab, and half a step stopped short of it
            # (0.07 mm under the mushroom's cap: a slit with odd small inside edges, T-359). Never past the
            # part's ends (a lip there would make a new face where a clamp or load sits).
            prism = extrude(poly, max(layer.z - THICKEN_STEP_MM - 0.02, 0.0), min(layer.z + THICKEN_STEP_MM + 0.02, top))
            if prism is not None:
                pieces.append((prism + [0, 0, base]) @ r)
    if not pieces:
        return []
    where = "where the strength check found the most stress" if hi - lo < 1 else f"along the {hi - lo:.0f} mm that carry the most stress"
    return [_edit("thicken-weak", "thin_neck", f"Make the weak spot {grow:.1f} mm thicker",
                  f"Adds {grow:.1f} mm of plastic all round {where} and {sleeve:.0f} mm past it either side, with a 45° end; "
                  "parts that are already thicker stay as they are. More cross-section is the direct fix when the plastic "
                  "itself is the limit. Check that nothing needs to fit there.", pieces, op="add", param="thicken_mm")]


def _same_outline(a, b, tol: float = 0.1) -> bool:
    """Two sections the same to about ``tol`` mm: bounds and area agree (no overlay: those can throw in
    Pyodide's GEOS on near-identical outlines, see checks._safe_diff)."""
    return bool(np.allclose(a.bounds, b.bounds, atol=tol) and abs(a.area - b.area) <= tol * max(a.length, b.length))


def _weak_axis(mesh: Mesh, spot: np.ndarray, hot_spots) -> tuple[np.ndarray, float, bool]:
    """Long direction of the member that is weak at ``spot``, its rough thickness, and whether the hot
    spots run along it (then the whole stretch between them is that member, see _weak_stretch).

    The worst spot is often where a neck or stem meets a wide part, and the solid round it there points
    across the wide part. So the candidates — the solid nearby (_member_axis), the hot spots' long
    direction and the axis of a flat disc of them (a stem in tension is hot all across) — are compared
    by their cross-section near the spot: the weak member is the narrow one."""
    candidates = [(_member_axis(mesh, spot, 2.5 * THICKEN_REACH_MM)[0], False)]
    if hot_spots is not None and len(hot_spots[0]) >= 8:
        pts = hot_spots[0]
        w, v = np.linalg.eigh(np.cov((pts - pts.mean(axis=0)).T))
        candidates.append((v[:, 2], bool(w[2] >= 4 * max(w[1], 1e-9))))
        if w[0] < 0.25 * w[1]:
            candidates.append((v[:, 0], False))
    # Another direction only wins when clearly narrower: a hot spot cloud a few degrees off a straight bar
    # would tilt the slabs, and tilted slabs cut into the bar's end faces (where it is clamped or loaded).
    best = None
    for a, joined in candidates:
        area = _narrowest(mesh, spot, a)
        if best is None or area < 0.85 * best[0]:
            best = (area, a, joined)
    return _true_axis(mesh, spot, best[1]), float(np.sqrt(best[0])), best[2]


def _true_axis(mesh: Mesh, spot: np.ndarray, axis: np.ndarray, within_deg: float = 10.0) -> np.ndarray:
    """``axis`` (from voxels and stress spots, a little off) set exactly along the member's side faces:
    the direction their normals are all square to, for a straight bar or a round pin. A sleeve a degree
    off the member it wraps came out ragged, in many small faces (T-357). Unchanged when the faces near
    the spot don't agree on one direction within ``within_deg``."""
    normals, areas = mesh.normals_and_areas()
    centres = mesh.triangles.mean(axis=1)
    near = np.linalg.norm(centres - spot, axis=1) <= 2.5 * THICKEN_REACH_MM
    side = near & (np.abs(normals @ axis) < np.sin(np.radians(2 * within_deg)))
    if areas[side].sum() <= 0:
        return axis
    n, a = normals[side], areas[side]
    w, v = np.linalg.eigh((n * a[:, None]).T @ n)
    if w[0] > 0.01 * w.sum():  # the sides don't share one axis (a tapered or bent member)
        return axis
    exact = v[:, 0] * np.sign(v[:, 0] @ axis)
    return exact if exact @ axis >= np.cos(np.radians(within_deg)) else axis


def _narrowest(mesh: Mesh, spot: np.ndarray, axis: np.ndarray, reach: float = THICKEN_REACH_MM) -> float:
    """Smallest area of the part's section across ``axis`` within ``reach`` of ``spot`` (the piece
    the spot sits on)."""
    from shapely.geometry import Point

    from .slicer import parts, section

    r = rotation_to_z(axis)
    turned = mesh.triangles @ r.T
    s = r @ spot
    zmin, zmax = turned[..., 2].min(axis=1), turned[..., 2].max(axis=1)
    best = np.inf
    for z in s[2] + np.arange(-reach, reach + 1e-9, 1.0):
        geom = section(turned[(zmin < z) & (zmax > z)], float(z))
        near = [p for p in parts(geom, 0.05) if p.distance(Point(s[:2])) <= 1.0] if not geom.is_empty else []
        if near:
            best = min(best, min(p.area for p in near))
    return best


def _weak_stretch(s: np.ndarray, r: np.ndarray, base: float, width: float, hot_spots, joined: bool = False) -> tuple[float, float]:
    """From where to where along the member (heights in the turned frame) it is nearly as weak as the
    spot ``s``: the hot spots across from it within ``width`` (in the member, not in a neighbour), as one
    run from the spot without gaps longer than THICKEN_REACH_MM — or, ``joined`` (the hot spots lie along
    the member, see _weak_axis), all of them: a neck under tension is weakest where it meets the wide
    ends, and everything between is the same neck."""
    if hot_spots is None:
        return s[2], s[2]
    h = hot_spots[0] @ r.T - [0, 0, base]
    z = np.sort(h[np.linalg.norm(h[:, :2] - s[:2], axis=1) <= width, 2])
    if joined and len(z):
        return min(z[0], s[2]), max(z[-1], s[2])
    lo = hi = s[2]
    for v in z[z > s[2]]:
        if v - hi > THICKEN_REACH_MM:
            break
        hi = v
    for v in z[z < s[2]][::-1]:
        if lo - v > THICKEN_REACH_MM:
            break
        lo = v
    return lo, hi


def _above_bed(turned: np.ndarray, r: np.ndarray, up):
    """For a layer height in the turned frame: the half-plane of that layer above the bed (the part's
    lowest point along ``up``), or None when the bed doesn't cut across layers (no ``up``, or the member
    stands upright: its slabs already end at the part's ends)."""
    if up is None:
        return None
    from shapely.geometry import Polygon

    u = r @ (np.asarray(up, float) / np.linalg.norm(up))
    across = np.hypot(u[0], u[1])
    if across < 1e-3:
        return None
    floor = float((turned.reshape(-1, 3) @ u).min())
    n, t = u[:2] / across, np.array([-u[1], u[0]]) / across
    big = 1e4

    def half(z: float):
        q = n * (floor - z * u[2]) / across  # on the bed line in this layer
        return Polygon([q - t * big, q + t * big, q + t * big + n * big, q - t * big + n * big])

    return half


def _member_axis(mesh: Mesh, spot: np.ndarray, reach: float) -> tuple[np.ndarray, float]:
    """Long direction of the part near ``spot`` (the main axis of the solid within ``reach``) and its
    thickness across (from the spread the other way: a bar of width w has variance w²/12)."""
    from .voxel import voxelize

    grid = voxelize(mesh, 20_000)
    solid = grid.centers(np.argwhere(grid.solid))
    centre = np.asarray(spot, float)
    axis, spread = np.array([0.0, 0.0, 1.0]), 0.0
    for _ in range(3):  # the spot is often on the surface or a corner: move the window to the member's middle
        pts = solid[np.linalg.norm(solid - centre, axis=1) <= reach]
        if len(pts) < 8:
            break
        w, v = np.linalg.eigh(np.cov((pts - pts.mean(axis=0)).T))
        axis, spread = v[:, -1], float(w[1])
        centre = pts.mean(axis=0)
    return axis, float(np.sqrt(12 * max(spread, 0.0)))


def concave_edges(mesh: Mesh, min_angle_deg: float = 30.0) -> list[dict]:
    """Sharp inside edges: two faces meeting at more than ``min_angle_deg`` with the second face in
    front of the first one's plane. Each: end points a, b; face normals n1, n2; t1, t2 the directions
    along each face away from the edge."""
    tris = mesh.triangles
    normals, _ = mesh.normals_and_areas()
    verts, inv = np.unique(np.round(tris.reshape(-1, 3), 4), axis=0, return_inverse=True)
    ids = inv.reshape(-1, 3)
    # Faces sharing an edge, vectorised (a Python loop over every half-edge is slow on big parts).
    # Sorting is stable, so equal edges keep face order; like a slicer's edge map, the 1st and 2nd
    # face on an edge pair up, then the 3rd and 4th (a non-manifold edge).
    edges = np.sort(np.stack([ids[:, [0, 1]], ids[:, [1, 2]], ids[:, [2, 0]]], axis=1).reshape(-1, 2), axis=1)
    order = np.lexsort((edges[:, 1], edges[:, 0]))
    e = edges[order]
    pos = np.arange(len(e))
    new = np.r_[True, np.any(e[1:] != e[:-1], axis=1)]
    run_start = np.maximum.accumulate(np.where(new, pos, 0))
    k = np.flatnonzero(~new & ((pos - run_start) % 2 == 1))
    second, first = order[k], order[k - 1]
    by_second = np.argsort(second)  # the order the edges complete in, face by face
    second, first = second[by_second], first[by_second]
    f1, f2 = first // 3, second // 3
    a, b = edges[second, 0], edges[second, 1]

    centroids = tris.mean(axis=1)
    n1, n2 = normals[f1], normals[f2]
    pa = verts[a]
    keep = ((a != b)  # a != b: drops sliver triangles (common after a boolean) whose corners round to one point
            & (np.einsum("ij,ij->i", n1, n2) <= np.cos(np.radians(min_angle_deg)))
            & (np.einsum("ij,ij->i", centroids[f2] - pa, n1) > 1e-6))  # second face behind the first: a convex edge
    a, b, f1, f2, n1, n2, pa = a[keep], b[keep], f1[keep], f2[keep], n1[keep], n2[keep], pa[keep]
    pb = verts[b]
    d = (pb - pa) / np.linalg.norm(pb - pa, axis=1, keepdims=True)
    t1, t2 = np.cross(n1, d), np.cross(n2, d)
    t1 *= np.where(np.einsum("ij,ij->i", centroids[f1] - pa, t1) > 0, 1.0, -1.0)[:, None]
    t2 *= np.where(np.einsum("ij,ij->i", centroids[f2] - pa, t2) > 0, 1.0, -1.0)[:, None]
    return [{"a": pa[i], "b": pb[i], "va": int(a[i]), "vb": int(b[i]), "n1": n1[i], "n2": n2[i], "t1": t1[i], "t2": t2[i]}
            for i in range(len(a))]


def _connected(edges: list[dict], seeds) -> list[int]:
    """Edges linked to ``seeds`` through shared vertices (a whole inside corner, e.g. around a stem)."""
    by_vertex: dict = {}
    for k, e in enumerate(edges):
        by_vertex.setdefault(e["va"], []).append(k)
        by_vertex.setdefault(e["vb"], []).append(k)
    seen, todo = set(), [int(k) for k in seeds]
    while todo:
        k = todo.pop()
        if k in seen:
            continue
        seen.add(k)
        for v in (edges[k]["va"], edges[k]["vb"]):
            todo += by_vertex[v]
    return sorted(seen)


def _corner_height(layers, p, inset=0.3) -> float:
    from shapely.geometry import Point

    top = layers[0].z
    for layer in layers:
        if layer.geom.is_empty or layer.geom.distance(Point(p)) > inset:
            break
        top = layer.z
    return top + 0.5


def _corner_edges(geom, p):
    """Unit vectors along the two outline edges leaving corner ``p``, and the shorter edge's length."""
    from shapely.geometry.polygon import orient as orient_poly

    from .slicer import parts

    for poly in parts(geom):
        c = np.asarray(orient_poly(poly.simplify(0.05)).exterior.coords)[:-1]
        d = np.linalg.norm(c - p, axis=1)
        i = int(np.argmin(d))
        if d[i] < 1e-3:
            e1, e2 = c[i - 1] - c[i], c[(i + 1) % len(c)] - c[i]
            l1, l2 = np.linalg.norm(e1), np.linalg.norm(e2)
            return e1 / l1, e2 / l2, float(min(l1, l2))
    return None, None, 0.0


def _prism(profile: list[np.ndarray], half: np.ndarray) -> np.ndarray:
    """Closed prism: polygon ``profile`` (convex, 3D points) swept ±``half``. Outward winding."""
    bot = [q - half for q in profile]
    top = [q + half for q in profile]
    n = len(profile)
    tris = []
    for i in range(1, n - 1):
        tris.append([bot[0], bot[i + 1], bot[i]])
        tris.append([top[0], top[i], top[i + 1]])
    for i in range(n):
        j = (i + 1) % n
        tris.append([bot[i], bot[j], top[j]])
        tris.append([bot[i], top[j], top[i]])
    t = np.array(tris)
    # Fix winding if the profile was clockwise around the sweep direction.
    if Mesh(t).volume() < 0:
        t = t[:, ::-1]
    return t


# ---------------------------------------------------------------- T-250 slots and flat gaps

GAP_OVERLAP_MM = 0.05  # slabs reach this far past the face, so cuts and unions don't leave a skin


def flat_pair(mesh: Mesh, region: np.ndarray, a: int, b: int) -> dict:
    """Two flat faces facing each other (a slot: the space between is empty) or facing away (a tab:
    plastic between): their distance, which one it is and the faces' normals and middles."""
    normals, areas = mesh.normals_and_areas()
    out = []
    for r in (a, b):
        m = region == r
        if not m.any():
            raise ValueError(f"Unknown face {r}")
        n = (normals[m] * areas[m, None]).sum(axis=0)
        n /= max(np.linalg.norm(n), 1e-12)
        if np.min(normals[m] @ n) < 0.999:
            raise ValueError("Pick flat faces")
        out.append((n, (mesh.triangles[m].mean(axis=1) * areas[m, None]).sum(axis=0) / areas[m].sum()))
    (na, ca), (nb, cb) = out
    if na @ nb > -0.99:
        raise ValueError("The two faces must be parallel and face opposite ways (the two sides of a slot or a tab)")
    dist = float(abs(na @ (cb - ca)))
    if dist < 1e-3:
        raise ValueError("The two faces lie in one plane")
    # Where the two faces overlap, as a rectangle on face a: for showing the gap and the fit on the part (T-386).
    u = np.cross(na, [1.0, 0, 0] if abs(na[0]) < 0.9 else [0, 1.0, 0])
    u /= np.linalg.norm(u)
    v = np.cross(na, u)
    spans = []
    for r in (a, b):
        pts = mesh.triangles[region == r].reshape(-1, 3) - ca
        spans.append((pts @ u, pts @ v))
    lo = [max(spans[0][k].min(), spans[1][k].min()) for k in (0, 1)]
    hi = [min(spans[0][k].max(), spans[1][k].max()) for k in (0, 1)]
    if hi[0] <= lo[0] or hi[1] <= lo[1]:  # they don't overlap: show face a's own extent
        lo, hi = [spans[0][k].min() for k in (0, 1)], [spans[0][k].max() for k in (0, 1)]
    plane = {"origin": ca.tolist(), "normal": na.tolist(), "u": u.tolist(), "v": v.tolist(),
             "u_range": [float(lo[0]), float(hi[0])], "v_range": [float(lo[1]), float(hi[1])]}
    return {"kind": "slot" if na @ (cb - ca) > 0 else "tab", "distance_mm": dist, "normal_a": na, "normal_b": nb,
            "centre_a": ca, "centre_b": cb, "plane": plane}


def gap_size(pair: dict, partner_mm: float, fit: str, up, profile=None, warp_mm: float = 0.0) -> tuple[float, str]:
    """Width to model a slot (or thickness of a tab) so a ``partner_mm`` part fits the ``fit`` way,
    and why. ``warp_mm``: how much the warping simulation says the gap closes as the part curls."""
    F = load_data("thresholds")["fits"]
    if fit not in F["gap_mm"]:
        raise ValueError(f"fit must be one of {tuple(F['gap_mm'])}")
    up = np.asarray(up, float) / np.linalg.norm(up)
    measured = (profile.material.get("fit_gap_mm") or {}) if profile is not None else {}
    across = abs(pair["normal_a"] @ up) > 0.9  # faces lying flat: the gap runs across the layers
    lh = profile.layer_height if profile is not None else 0.2
    gap = measured.get(fit, F["gap_mm"][fit]) + (F["sideways_extra_mm"] if across else 0.0) + max(warp_mm, 0.0)
    if across:
        gap = max(gap, lh)
    slot = pair["kind"] == "slot"
    size = partner_mm + gap if slot else partner_mm - gap
    what = (f"For a {partner_mm:g} mm thick part to {FIT_NAMES[fit]}, make this slot {size:.2f} mm wide" if slot else
            f"For this tab to {FIT_NAMES[fit]} into a {partner_mm:g} mm slot, make it {size:.2f} mm thick")
    why = f" (now {pair['distance_mm']:.2f}: {size - pair['distance_mm']:+.2f} mm, half on each face)."
    if across:
        why += (" The faces lie flat when printed, so the gap runs across the layers: the upper face sags a little over it "
                f"and the printer stops on whole layers ({lh} mm). That's included.")
    if warp_mm > 0.02:
        why += f" The warping simulation shows the gap closing by about {warp_mm:.2f} mm as the part curls (rough): added."
    note = (" Using the gaps from your fit test print." if measured.get(fit) is not None
            else " Printers differ by a tenth or two: the fit test print (My materials) gives your printer's numbers.")
    return size, what + why + note


def _gap_edits(mesh: Mesh, region: np.ndarray, gaps: dict, up, profile=None) -> list[dict]:
    """Move both faces of each sized slot or tab by half the change: a slab added onto the face or
    cut into it, the face's own outline swept along its normal."""
    F = load_data("thresholds")["fits"]
    out = []
    for (a, b), want in gaps.items():
        pair = flat_pair(mesh, region, a, b)
        size, why = gap_size(pair, want["partner_mm"], want["fit"], up, profile, want.get("warp_mm", 0.0))
        change = size - pair["distance_mm"]
        if abs(change) < F["min_change_mm"]:
            continue
        move = (-change if pair["kind"] == "slot" else change) / 2  # along each face's normal: + adds plastic
        lo, hi = (-GAP_OVERLAP_MM, move) if move > 0 else (move, GAP_OVERLAP_MM)
        pieces = [p for r in (a, b) for p in _face_slab(mesh, region == r, lo, hi)]
        if not pieces:
            continue
        name = "slot" if pair["kind"] == "slot" else "tab"
        out.append(_edit(f"gap-{a}-{b}", "fit_size", f"{pair['distance_mm']:.2f} → {size:.2f} mm {name} for a {want['fit']} fit",
                         why, pieces, op="add" if move > 0 else "cut"))
    return out


def _face_slab(mesh: Mesh, mask: np.ndarray, lo: float, hi: float) -> list[np.ndarray]:
    """Closed prisms of a flat face's outline from ``lo`` to ``hi`` mm along its normal."""
    import shapely

    from .additions import extrude

    normals, areas = mesh.normals_and_areas()
    n = (normals[mask] * areas[mask, None]).sum(axis=0)
    n /= np.linalg.norm(n)
    u = np.cross(n, [1.0, 0, 0] if abs(n[0]) < 0.9 else [0, 1.0, 0])
    u /= np.linalg.norm(u)
    v = np.cross(n, u)
    tris = mesh.triangles[mask]
    origin = tris[0, 0]
    flat = np.stack([(tris - origin) @ u, (tris - origin) @ v], axis=-1)
    polys = shapely.polygons(flat)
    face = shapely.union_all(shapely.make_valid(polys[shapely.area(polys) > 1e-9]), grid_size=1e-4)
    out = []
    for poly in getattr(face, "geoms", [face]):
        if poly.geom_type != "Polygon" or poly.area < 0.01:
            continue
        prism = extrude(poly.simplify(0.01), lo, hi)
        if prism is not None:
            out.append(origin + prism[..., :1] * u + prism[..., 1:2] * v + prism[..., 2:] * n)
    return out


# ---------- screws, nuts and heat-set inserts (T-320) ----------

FASTENER_USES = {
    "screw": "a screw cut straight into the plastic",
    "insert": "a heat-set insert",
    "nut_top": "a bolt with a nut in a pocket",
    "nut_side": "a bolt with a nut pushed in from the side",
    "bolt": "a bolt going through",
}


def _frame(f: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Centre and three unit vectors (u, s across the hole, axis along it), as ``_ring`` uses them."""
    axis, c = np.asarray(f["axis"], float), np.asarray(f["center"], float)
    u = np.cross(axis, [1.0, 0, 0] if abs(axis[0]) < 0.9 else [0, 1.0, 0])
    u /= np.linalg.norm(u)
    return c, u, np.cross(axis, u), axis


def _axis_prism(f: dict, shape, z0: float, z1: float) -> np.ndarray:
    """A 2D ``shape`` (hole frame: x along u, y along s) extruded from z0 to z1 along the hole axis."""
    from .additions import extrude

    c, u, s, axis = _frame(f)
    local = extrude(shape, z0, z1)
    return c + local[..., :1] * u + local[..., 1:2] * s + local[..., 2:] * axis


def _section(mesh: Mesh, f: dict):
    """The part cut across the hole at its middle, in the hole frame (the hole at the origin)."""
    from .slicer import section

    c, u, s, axis = _frame(f)
    turned = (mesh.triangles - c) @ np.stack([u, s, axis]).T
    return section(turned, 0.0)


def hole_wall(mesh: Mesh, f: dict) -> float | None:
    """Thinnest plastic around a hole at its middle (to the outside or the next hole), mm."""
    from shapely.geometry import Point

    geom = _section(mesh, f)
    centre, r = Point(0, 0), f["radius"]
    for piece in getattr(geom, "geoms", [geom]):
        if piece.is_empty or piece.geom_type != "Polygon" or not piece.buffer(r + 0.3).contains(centre):
            continue
        rings = [piece.exterior, *piece.interiors]
        others = [g for g in rings if abs(g.distance(centre) - r) > 0.25 * r + 0.1]  # not the hole's own wall
        if others:
            return float(min(g.distance(centre) for g in others) - r)
    return None


def _top_end(f: dict, up) -> float:
    """+1 / -1: the end of the hole that is higher when printed (where a pocket is open to the top)."""
    return 1.0 if float(np.asarray(f["axis"]) @ np.asarray(up, float)) >= -1e-6 else -1.0


def _hexagon(af: float, point_up=None):
    """Hexagon ``af`` across the flats at the origin; with ``point_up`` (2D) a corner points that way
    (a sideways pocket then has a pointed roof, which prints without sagging)."""
    from shapely.geometry import Polygon

    r = af / np.sqrt(3)  # centre to corner
    start = np.arctan2(point_up[1], point_up[0]) if point_up is not None else 0.0
    a = start + np.arange(6) * np.pi / 3
    return Polygon(np.column_stack([r * np.cos(a), r * np.sin(a)]))


def fastener_plan(mesh: Mesh, f: dict, size: str, use: str, up, profile) -> dict:
    """What a hole needs for ``use`` (FASTENER_USES) with a ``size`` (e.g. "M3") screw: the diameter to
    model, depth, wall, the nut pocket, and the slicer fix when the design can't change. Plain words."""
    T = load_data("thresholds")
    F, fits = T["fasteners"], T["fits"]
    if not isinstance(F.get(size), dict):
        raise ValueError(f"size must be one of {[k for k, v in F.items() if isinstance(v, dict)]}")
    if use not in FASTENER_USES:
        raise ValueError(f"use must be one of {tuple(FASTENER_USES)}")
    if f["type"] != "hole":
        raise ValueError("Pick a round hole for a screw, insert or nut")
    s = F[size]
    up = np.asarray(up, float) / np.linalg.norm(up)
    sideways = abs(np.asarray(f["axis"]) @ up) < 0.7
    measured = (profile.material.get("fit_gap_mm") or {}) if profile is not None else {}
    # Holes something passes through: the printer's own shrink from the fit test print, if there is one.
    shrink = measured["sliding"] - fits["gap_mm"]["sliding"] if measured.get("sliding") is not None else 0.0
    passes = use in ("bolt", "nut_top", "nut_side")
    if passes:
        target = s["clearance_normal_mm"] + shrink + (fits["sideways_extra_mm"] if sideways else 0.0)
    else:
        target = s["pilot_mm"] if use == "screw" else s["insert_hole_mm"]
    lines = [f"For {FASTENER_USES[use]} ({size}), model this hole Ø{target:.2f} mm (now Ø{f['diameter']:.2f})."]
    if passes and shrink:
        lines.append("That includes the gap from your fit test print.")
    if passes and sideways:
        lines.append("It lies on its side when printed, so it comes out a little smaller at the top: that's included.")
    need_depth = None
    if use == "screw":
        need_depth = F["engage_d"] * s["d"]
        lines.append(f"Let the screw bite at least {need_depth:g} mm deep (twice its diameter); screws made for "
                     "plastic hold best. Don't overtighten: the thread is only plastic.")
    elif use == "insert":
        need_depth = s["insert_length_mm"] + F["insert_extra_depth_mm"]
        lines.append(f"Make the hole at least {need_depth:.1f} mm deep (the {size} insert is {s['insert_length_mm']:g} mm "
                     "long), with a small chamfer at the top to start the insert straight. Press it in with the "
                     "soldering iron at the insert maker's temperature. Printed standing up, the insert pulls across "
                     "the layers instead of between them, which holds best.")
    depth = float(f["length"])
    if need_depth is not None and depth + 0.05 < need_depth:
        lines.append(f"The hole is only {depth:.1f} mm long here: make it deeper or the boss taller.")
    wall = hole_wall(mesh, f)
    need_wall = {"screw": s["d"], "insert": 2.0}.get(use, 0.0)
    walls = None
    if wall is not None and need_wall:
        left = wall - (target - f["diameter"]) / 2  # the resize eats into (or adds to) the wall
        if left < need_wall:
            lw = profile.line_width if profile is not None else 0.45
            walls = int(np.ceil(need_wall / lw))
            lines.append(f"Only {max(left, 0):.1f} mm of plastic around it; aim for about {need_wall:g} mm. Add a boss "
                         f"(offered below), or if the design can't change: print {walls} walls so it's solid plastic, "
                         "not infill.")
    pocket = None
    if use == "nut_top":
        af, t = s["nut_af_mm"] + F["nut_clearance_mm"] + 0.1, s["nut_thickness_mm"] + 0.2
        pocket = {"kind": "top", "across_flats_mm": round(af, 2), "depth_mm": round(t, 2)}
        lines.append(f"Nut pocket at the upper end: {af:.1f} mm across the flats, {t:.1f} mm deep (the nut is "
                     f"{s['nut_af_mm']:g} × {s['nut_thickness_mm']:g} mm; printed pockets come out small).")
    elif use == "nut_side":
        af, t = s["nut_af_mm"] + F["nut_slot_extra_mm"], s["nut_thickness_mm"] + F["nut_slot_extra_mm"]
        pocket = {"kind": "side", "across_flats_mm": round(af, 2), "depth_mm": round(t, 2)}
        lines.append(f"Slot from the side for the nut: {af:.1f} mm wide, {t:.1f} mm high, from the outside to the "
                     "middle of the hole. The nut slides in and lines up with the bolt.")
    elif use == "bolt":
        lines.append(f"Leave a flat face about Ø{s['washer_od_mm']:g} mm round the hole for the bolt head or washer.")
    return {"size": size, "use": use, "target_mm": round(target, 2), "modelled_mm": round(f["diameter"], 2),
            "depth_mm": round(depth, 2), "need_depth_mm": need_depth,
            "wall_mm": None if wall is None else round(wall, 2), "need_wall_mm": need_wall or None,
            "walls": walls, "pocket": pocket, "explanation": " ".join(lines)}


def _side_reach(geom, d: np.ndarray, far: float) -> float:
    """Distance from the hole centre to the part's outline along ``d`` (hole frame): where a slot comes out."""
    if geom.is_empty:
        return far
    hit = LineString([(0, 0), tuple(d * far)]).intersection(geom.boundary)
    pts = [q for q in shapely.get_parts(hit) if q.geom_type == "Point"] if not hit.is_empty else []
    return max((float(np.hypot(q.x, q.y)) for q in pts), default=far)


def _fastener_edits(mesh: Mesh, features: list[dict], fasteners: dict, up, profile=None) -> list[dict]:
    """The design changes for each hole the user said a fastener goes in: resize, boss, nut pocket or slot."""
    from shapely import affinity
    from shapely.geometry import Point, box

    min_change = load_data("thresholds")["fits"]["min_change_mm"]
    up = np.asarray(up, float) / np.linalg.norm(up)
    out = []
    for f in features:
        want = fasteners.get(f["region"])
        if not want or f["type"] != "hole":
            continue
        plan = fastener_plan(mesh, f, want["size"], want["use"], up, profile)
        size, rid, length = plan["target_mm"], f["region"], f["length"]
        what = FASTENER_USES[plan["use"]]
        if abs(size - f["diameter"]) >= min_change:
            r_old, r_new = f["radius"], size / 2
            smaller = r_new < r_old
            ring = Point(0, 0).buffer(r_old + 0.3 if smaller else r_new, 64)  # 0.3: reach into the plastic to join
            if smaller:
                ring = ring.difference(Point(0, 0).buffer(r_new, 64))
            out.append(_edit(f"fastener-{rid}", "fastener", f"Ø{f['diameter']:.2f} → Ø{size:.2f} mm hole for {what} ({plan['size']})",
                             plan["explanation"], [_axis_prism(f, ring, *_ends(f, smaller))], op="add" if smaller else "cut"))
        if plan["walls"]:
            boss = Point(0, 0).buffer(size / 2 + plan["need_wall_mm"], 64).difference(Point(0, 0).buffer(size / 2, 64))
            out.append(_edit(f"fastener-boss-{rid}", "fastener", f"Boss with {plan['need_wall_mm']:g} mm of plastic round the {plan['size']} hole",
                             "Adds a ring of plastic round the hole where the wall is thin, so the screw or insert can't "
                             "split it. Check that nothing else needs that space.",
                             [_axis_prism(f, boss, *_ends(f, True))], op="add"))
        p = plan["pocket"]
        if p is None:
            continue
        _, u, s, _ = _frame(f)
        side_up = np.array([up @ u, up @ s])
        point_up = side_up / np.linalg.norm(side_up) if np.linalg.norm(side_up) > 0.3 else None  # lying: corner on top
        hexa = _hexagon(p["across_flats_mm"], point_up)
        if p["kind"] == "top":
            end = _top_end(f, up) * length / 2
            z0, z1 = (end - p["depth_mm"], end + 0.5) if end > 0 else (end - 0.5, end + p["depth_mm"])
            out.append(_edit(f"fastener-nut-{rid}", "fastener", f"Nut pocket for the {plan['size']} nut",
                             f"A hexagonal pocket {p['across_flats_mm']:.1f} mm across the flats and {p['depth_mm']:.1f} mm "
                             "deep at the hole's upper end: the nut drops in and can't turn.", [_axis_prism(f, hexa, z0, z1)]))
            continue
        # A slot out to the nearest outside, along one of the four directions across the hole (not
        # straight up: its open end would be a long bridge).
        geom = _section(mesh, f)
        far = float(np.ptp(mesh.triangles.reshape(-1, 3), axis=0).max())
        dirs = [np.array([np.cos(a), np.sin(a)]) for a in np.radians([0, 90, 180, 270])]
        dirs = [d for d in dirs if point_up is None or d @ point_up < 0.7]
        reach, d = min((_side_reach(geom, d, far), tuple(d)) for d in dirs)
        w = p["across_flats_mm"]
        slot = hexa.union(affinity.rotate(box(0, -w / 2, reach + 1.0, w / 2), np.degrees(np.arctan2(d[1], d[0])), origin=(0, 0)))
        out.append(_edit(f"fastener-nut-{rid}", "fastener", f"Nut slot for the {plan['size']} nut, from the side",
                         f"A slot {w:.1f} mm wide and {p['depth_mm']:.1f} mm high from the outside to the middle of the hole: "
                         "push the nut in from the side and it lines up with the bolt.",
                         [_axis_prism(f, slot, -p["depth_mm"] / 2, p["depth_mm"] / 2)]))
    return out


FASTENER_SUGGEST_TOL = 0.3  # mm: within this of a standard pilot, clearance, insert or nominal size
_FASTENER_LABELS = {"screw": "Screw into plastic", "bolt": "Through bolt", "insert": "Heat-set insert",
                    "nut_top": "Nut from top", "nut_side": "Nut from side"}
_COUNT_WORDS = {2: "Both", 3: "All three", 4: "All four", 5: "All five", 6: "All six",
                7: "All seven", 8: "All eight", 9: "All nine", 10: "All ten"}
_MATCH_FIELDS = (("d", "nominal"), ("pilot_mm", "pilot"), ("clearance_close_mm", "clearance"),
                 ("clearance_normal_mm", "clearance"), ("insert_hole_mm", "insert"))


def suggest_fasteners(mesh: Mesh, features: list[dict], up, profile=None) -> list[dict]:
    """Group equal round holes near standard metric sizes (``fasteners`` in thresholds.yaml) and
    return one suggestion per group with the likely uses sized via ``fastener_plan``."""
    F = {k: v for k, v in load_data("thresholds")["fasteners"].items() if isinstance(v, dict)}
    up = np.asarray(up, float) / np.linalg.norm(up)
    matched = []
    for f in accepted(features):
        if f["type"] != "hole" or not f["full_circle"]:
            continue
        d = float(f["diameter"])
        # Prefer common whole M-sizes over M2.5, and screw/bolt sizes over insert holes when tied.
        hits = sorted(
            (size == "M2.5", kind == "insert", abs(d - s[field]), size, kind)
            for size, s in F.items() for field, kind in _MATCH_FIELDS
            if abs(d - s[field]) <= FASTENER_SUGGEST_TOL + 1e-6
        )
        if not hits:
            continue
        _, _, _, size, kind = hits[0]
        sideways = abs(np.asarray(f["axis"]) @ up) < 0.7
        matched.append((size, sideways, d, kind, f))
    matched.sort(key=lambda x: (x[0], x[1], x[2]))
    groups: list[list] = []
    for item in matched:
        if groups and groups[-1][0][:2] == item[:2] and item[2] - groups[-1][0][2] <= 0.1 + 1e-6:
            groups[-1].append(item)
        else:
            groups.append([item])
    out = []
    for grp in groups:
        size, _, _, kind, rep = grp[0]
        dia = float(np.mean([x[2] for x in grp]))
        if kind == "insert":
            uses = ("insert",)
        elif kind == "pilot" or (kind == "nominal" and dia < F[size]["d"] - 0.05):
            uses = ("screw", "bolt", "insert", "nut_top", "nut_side")
        else:
            uses = ("bolt", "screw", "insert", "nut_top", "nut_side")
        choices = [{"use": u, "label": _FASTENER_LABELS[u],
                    "target_mm": fastener_plan(mesh, rep, size, u, up, profile)["target_mm"]} for u in uses]
        n, lead = len(grp), choices[0]
        head = f"Ø{dia:.1f} mm hole" if n == 1 else f"{_COUNT_WORDS.get(n, f'All {n}')} Ø{dia:.1f} mm holes"
        t = lead["target_mm"]
        tgt = f"{t:.1f}" if abs(t * 10 - round(t * 10)) < 1e-6 else f"{t:.2f}"
        if lead["use"] == "screw":
            prompt = f"{head}: is an {size} screw going in? Then model Ø{tgt} mm to screw into plastic (or pick another use)."
        elif lead["use"] == "insert":
            prompt = f"{head}: is an {size} heat-set insert going in? Then model Ø{tgt} mm."
        else:
            prompt = f"{head}: is an {size} screw going through? Then model Ø{tgt} mm (or pick how it's held)."
        regions = sorted(x[4]["region"] for x in grp)
        out.append({"size": size, "diameter_mm": round(dia, 2), "regions": regions, "count": n,
                    "label": head, "prompt": prompt, "choices": choices})
    return out


# ---------- modelled threads (T-323) ----------

# ISO metric coarse pitches (ISO 261): the size a thread's outer diameter is matched to.
METRIC_COARSE = {"M2": 0.4, "M2.5": 0.45, "M3": 0.5, "M4": 0.7, "M5": 0.8, "M6": 1.0, "M8": 1.25, "M10": 1.5,
                 "M12": 1.75, "M16": 2.0, "M20": 2.5, "M24": 3.0}


def find_threads(mesh: Mesh, axes=None) -> list[dict]:
    """Threads modelled into the part. A 60° thread's flanks lean 30° off the plane across its axis, so
    their normals sit about 30° from the axis, pointing alternately up and down it; and the flank faces
    climb one pitch per turn round the axis. Each: axis, centre, major / minor diameter, pitch, length,
    "external" (a bolt) or internal (a nut / tapped hole), and the ISO size it is closest to.
    ``axes``: directions to look along (default: the model's x, y, z, where CAD puts threads)."""
    normals, areas = mesh.normals_and_areas()
    cents = mesh.triangles.mean(axis=1)
    out = []
    for a in (np.eye(3) if axes is None else np.asarray(axes, float)):
        a = a / np.linalg.norm(a)
        along = normals @ a
        flank = (np.abs(along) > 0.75) & (np.abs(along) < 0.95)
        if flank.sum() < 60:
            continue
        idx = np.flatnonzero(flank)
        u = np.cross(a, [1.0, 0, 0] if abs(a[0]) < 0.9 else [0, 1.0, 0])
        u /= np.linalg.norm(u)
        v = np.cross(a, u)
        p2 = np.column_stack([cents[idx] @ u, cents[idx] @ v])
        for group in _clusters(p2):
            t = idx[group]
            q = p2[group]
            centre = q.mean(axis=0)
            rel = q - centre
            r = np.hypot(rel[:, 0], rel[:, 1])
            theta = np.arctan2(rel[:, 1], rel[:, 0])
            z = cents[t] @ a
            if np.ptp(theta) < 5.5 or r.max() - r.min() < 0.1:  # not all the way round, or no depth: not a thread
                continue
            radial = (normals[t] @ u) * rel[:, 0] / np.maximum(r, 1e-9) + (normals[t] @ v) * rel[:, 1] / np.maximum(r, 1e-9)
            best = None
            for pitch in sorted(set(METRIC_COARSE.values()) | {0.35, 0.75}):
                for hand in (1, -1):
                    # Upward flanks of one thread: z − hand·θ/2π·P is the same (mod P) on all of them.
                    up_f = along[t] > 0
                    if up_f.sum() < 20:
                        continue
                    phase = 2 * np.pi * (z[up_f] / pitch - hand * theta[up_f] / (2 * np.pi))
                    w = areas[t][up_f]
                    score = float(np.hypot((w * np.cos(phase)).sum(), (w * np.sin(phase)).sum()) / w.sum())
                    if best is None or score > best[0]:
                        best = (score, pitch, hand)
            if best is None or best[0] < 0.8:
                continue
            _, pitch, hand = best
            major, minor = 2 * float(np.percentile(r, 98)), 2 * float(np.percentile(r, 2))
            depth = (major - minor) / 2
            if not 0.3 * pitch <= depth <= 1.0 * pitch:
                continue
            size = min(METRIC_COARSE, key=lambda k: abs(float(k[1:]) - major))
            external = float(np.average(radial, weights=areas[t])) > 0
            c3 = centre[0] * u + centre[1] * v
            out.append({"axis": a.tolist(), "center": (c3 + a * z.mean()).tolist(), "major_mm": round(major, 2),
                        "minor_mm": round(minor, 2), "pitch_mm": pitch, "length_mm": round(float(np.ptp(z)), 1),
                        "external": external, "left_hand": hand < 0,
                        "size": size if abs(float(size[1:]) - major) < 0.6 else None,
                        "lo": (mesh.triangles[t].reshape(-1, 3).min(axis=0)).tolist(),
                        "hi": (mesh.triangles[t].reshape(-1, 3).max(axis=0)).tolist()})
    return out


def _clusters(p2: np.ndarray, link: float = 1.0) -> list[np.ndarray]:
    """Groups of 2D points closer than ``link`` (grid cells, then joined neighbours): one per thread."""
    cell = np.floor(p2 / link).astype(np.int64)
    keys, inv = np.unique(cell, axis=0, return_inverse=True)
    inv = inv.ravel()
    lookup = {tuple(k): i for i, k in enumerate(keys)}
    parent = list(range(len(keys)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, k in enumerate(keys):
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                j = lookup.get((k[0] + dx, k[1] + dy))
                if j is not None:
                    a, b = root(i), root(j)
                    if a != b:
                        parent[max(a, b)] = min(a, b)
    roots = np.array([root(i) for i in range(len(keys))])[inv]
    return [np.flatnonzero(roots == r) for r in np.unique(roots)]
