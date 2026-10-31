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

from .checks import orient
from .mesh import Mesh
from .orientation import rotation_to_z
from .profile import load_data
from .slicer import slice_mesh

TEARDROP_MIN_DIAMETER = 5.0  # smaller horizontal holes bridge fine


TAGS = ("hole", "boss", "none")  # what the user can say a detected feature is ("none": not a feature)


def detect_features(mesh: Mesh, region: np.ndarray, tags: dict | None = None) -> list[dict]:
    """Round holes and bosses, one per curved region. ``tags`` ({region: tag}) are the user's
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
    ang = np.arctan2(p2[:, 1] - cy, p2[:, 0] - cx)
    coverage = len(np.unique(np.floor((ang + np.pi) / (2 * np.pi) * 36))) / 36
    return {
        "type": "hole" if inward > 0.5 else "boss",
        "axis": axis.tolist(),
        "center": center.tolist(),
        "radius": radius,
        "diameter": 2 * radius,
        "length": float(t.max() - t.min()),
        "full_circle": coverage > 0.9,
        "fit_error": float(resid.max() / radius),
    }


# Adjustable edit sizes: default, min, max (the UI shows these as inputs).
CORNER_ROUND_SHARE = 0.3  # low-warp materials: round corners once the footprint is this share of the warp length
CORNER_ARC_STEPS = 8
OFF_CORNER = 0.01  # mm cutters keep from the part's corner edges
MAX_FOOT_POINTS = 600  # outline points; more and the slicer's elephant foot compensation is the better tool
CHAMFER_REACH_VOXELS = 8  # the nearest inside edge this close to the weak spot (in analysis voxels) seeds the chamfer
MAX_FILLET_PIECES = 400

PARAMS = {
    "chamfer_mm": (3.0, 0.5, 10.0),  # radius of rounded bed corners
    "foot_chamfer_mm": (0.5, 0.2, 1.5),  # elephant-foot chamfer along the bottom edge
    "stress_chamfer_mm": (2.0, 0.5, 8.0),  # 45° chamfer at the inside corner where the strength check finds the most stress
    "teardrop_angle_deg": (45.0, 30.0, 60.0),  # tip half-angle from vertical; 45° is the classic choice
    "ledge_max_mm": (20.0, 2.0, 40.0),  # widest ledge to fill in at 45° underneath
    "wall_lines": (2.0, 2.0, 4.0),  # thin walls grow to this many printed lines
}


def edit_params(params: dict | None) -> dict:
    """Defaults filled in and values clamped to their range."""
    out = {}
    for k, (default, lo, hi) in PARAMS.items():
        v = float((params or {}).get(k, default))
        out[k] = min(max(v, lo), hi)
    return out


def _edit(id_, rule, title, description, pieces: list[np.ndarray], op="cut", param=None) -> dict:
    return {
        "id": id_, "rule": rule, "title": title, "description": description, "op": op, "param": param,
        "mesh": np.concatenate(pieces).astype("<f4").tobytes(),
        "pieces": [len(p) for p in pieces],
    }


def design_edits(mesh: Mesh, region: np.ndarray, up, profile, params: dict | None = None, tags: dict | None = None,
                 weak_spot=None, voxel_mm: float = 1.0, fits: dict | None = None) -> list[dict]:
    """Suggested geometry changes for this build direction, as closed meshes in part coordinates.
    ``weak_spot`` (part coordinates, from the last strength check) adds a chamfer at the nearest inside corner.
    ``fits`` ({region: {"partner_mm", "fit"}}) resizes holes and posts for what goes into them."""
    from .additions import ledge_chamfers, sacrificial_layers, thicken_walls

    up = np.asarray(up, float) / np.linalg.norm(up)
    prm = edit_params(params)
    angle = prm["teardrop_angle_deg"]
    edits = []
    for f in accepted(detect_features(mesh, region, tags)):
        axis = np.asarray(f["axis"])
        if f["type"] == "hole" and f["full_circle"] and abs(axis @ up) < 0.2 and f["diameter"] >= TEARDROP_MIN_DIAMETER:
            edits.append(_edit(
                f"teardrop-{f['region']}", "short_bridge", f"Teardrop for the horizontal Ø{f['diameter']:.1f} mm hole",
                f"Makes the top of the hole a {angle:.0f}° point so it prints without sagging or support.",
                [_teardrop_cutter(f, up, angle)], param="teardrop_angle_deg"))
    edits += _fit_edits(accepted(detect_features(mesh, region, tags)), fits or {}, up, profile)
    r = rotation_to_z(up)
    oriented = orient(mesh, r)
    to_part = _to_part(mesh, r)
    edits += _corner_chamfers(oriented, to_part, profile, prm["chamfer_mm"])
    edits += _foot_chamfer(oriented, to_part, profile, prm["foot_chamfer_mm"])
    edits += _stress_chamfers(mesh, weak_spot, voxel_mm, prm["stress_chamfer_mm"])
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
    for i, w in enumerate(thicken_walls(oriented, profile, prm["wall_lines"])):
        edits.append(_edit(
            f"wall-{i}", "single_line_wall", f"Thicken the {w['width']:.2f} mm wall to {w['target']:.2f} mm",
            f"Grows the wall at {w['z0']:.0f}–{w['z1']:.0f} mm to {prm['wall_lines']:.0f} printed lines so it prints solid and is "
            "much stronger. It grows on both sides; check that nothing needs to fit there.",
            [to_part(p) for p in w["pieces"]], op="add", param="wall_lines"))
    return edits


FIT_NAMES = {"press": "be pressed in (stays put)", "snug": "push in snugly", "sliding": "slide and turn freely", "loose": "drop in loosely"}


def fit_size(feature: dict, partner_mm: float, fit: str, up, profile=None) -> tuple[float, str]:
    """Diameter to model a hole (or round post) so a ``partner_mm`` part fits it the ``fit`` way,
    and why, in plain words."""
    F = load_data("thresholds")["fits"]
    if fit not in F["gap_mm"]:
        raise ValueError(f"fit must be one of {tuple(F['gap_mm'])}")
    up = np.asarray(up, float) / np.linalg.norm(up)
    sideways = abs(np.asarray(feature["axis"]) @ up) < 0.7
    measured = (profile.material.get("fit_gap_mm") or {}) if profile is not None else {}  # from the fit test print
    gap = measured.get(fit, F["gap_mm"][fit]) + (F["sideways_extra_mm"] if sideways else 0.0)
    hole = feature["type"] == "hole"
    size = partner_mm + gap if hole else partner_mm - gap
    what = (f"For a Ø{partner_mm:g} mm part to {FIT_NAMES[fit]}, model this hole Ø{size:.2f} mm" if hole else
            f"For this post to {FIT_NAMES[fit]} into a Ø{partner_mm:g} mm hole, model it Ø{size:.2f} mm")
    why = f" (now Ø{feature['diameter']:.2f}: {size - feature['diameter']:+.2f} mm)."
    if sideways:
        why += " It lies on its side when printed, so it comes out a little smaller at the top: that's included."
    note = (" Using the gaps from your fit test print." if measured.get(fit) is not None
            else " Printers differ by a tenth or two: the fit test print (My materials) gives your printer's numbers.")
    return size, what + why + note


def _fit_edits(features: list[dict], fits: dict, up, profile=None) -> list[dict]:
    """Resize holes and posts the user said something fits into: a ring cut away or added."""
    F = load_data("thresholds")["fits"]
    out = []
    for f in features:
        want = fits.get(f["region"])
        if not want:
            continue
        size, why = fit_size(f, want["partner_mm"], want["fit"], up, profile)
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
                         why, [_ring(f, inner, outer)], op="add" if grow_part else "cut"))
    return out


def _ring(f: dict, inner: float, outer: float) -> np.ndarray:
    """A ring (or disc, inner = 0) along a hole / post's axis, over its length."""
    from shapely.geometry import Point

    from .additions import extrude

    shape = Point(0, 0).buffer(outer, 64)
    if inner > 0:
        shape = shape.difference(Point(0, 0).buffer(inner, 64))
    axis, c = np.asarray(f["axis"]), np.asarray(f["center"])
    u = np.cross(axis, [1.0, 0, 0] if abs(axis[0]) < 0.9 else [0, 1.0, 0])
    u /= np.linalg.norm(u)
    s = np.cross(axis, u)
    local = extrude(shape, -f["length"] / 2, f["length"] / 2)
    return c + local[..., :1] * u + local[..., 1:2] * s + local[..., 2:] * axis


def _to_part(mesh: Mesh, r: np.ndarray):
    """Map triangles from the oriented part (orient(mesh, r)) back to part coordinates."""
    pts = mesh.triangles.reshape(-1, 3) @ r.T
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    shift = np.array([-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]])
    return lambda tris: ((np.asarray(tris).reshape(-1, 3) - shift) @ r).reshape(-1, 3, 3)  # r.T @ v per point


def _teardrop_cutter(f: dict, up: np.ndarray, angle_deg: float = 45.0) -> np.ndarray:
    """Kite-shaped prism (centre, two tangent points, apex) along the hole axis. The roof
    lines are ``angle_deg`` from vertical: they touch the circle at r·(sin θ up ± cos θ side)
    and meet at r / sin θ above the centre. The tangent points sit a hair outside the circle and the
    prism a hair past both ends of the hole: touching the hole's own corners and end faces exactly
    leaves zero-thickness fins in the result."""
    axis, c, r = np.asarray(f["axis"]), np.asarray(f["center"]), f["radius"]
    u = up - (up @ axis) * axis
    u /= np.linalg.norm(u)
    s = np.cross(axis, u)
    th = np.radians(angle_deg)
    rt = r + OFF_CORNER
    up_k, side_k = rt * np.sin(th), rt * np.cos(th)
    profile = [c - 0.5 * r * u, c + side_k * s + up_k * u, c + r / np.sin(th) * u, c - side_k * s + up_k * u]
    return _prism(profile, axis * (f["length"] / 2 + 5 * OFF_CORNER))


def _corner_chamfers(oriented: Mesh, to_part, profile, size: float = 3.0) -> list[dict]:
    """Round sharp footprint corners (where warping starts): a vertical fillet of radius ``size``,
    cut as a fan of thin wedges between the corner and the arc. Offered for warp-prone materials,
    and for others once the footprint is a fair part of the length at which they start to lift."""
    from .checks import _rect_sides, _sharp_corners

    layers = slice_mesh(oriented, profile.layer_height, max_layers=60)
    first = layers[0].geom
    if first.is_empty:
        return []
    corners = _sharp_corners(first)
    if not corners:
        return []
    if profile.material["warp_risk"] == "low":
        risk = load_data("thresholds")["warping"]["risk_length_mm"]
        limit = risk.get(profile.material.get("based_on", profile.material_id), {}).get(profile.enclosure)
        if not limit or _rect_sides(first.minimum_rotated_rectangle)[1] < CORNER_ROUND_SHARE * limit:
            return []
    out = []
    for i, (x, y) in enumerate(corners):
        top = _corner_height(layers, np.array([x, y]))
        e1, e2, edge = _corner_edges(first, np.array([x, y]))
        if e1 is None:
            continue
        p = np.array([x, y])
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
        out.append(_edit(
            f"chamfer-{i}", "sharp_bed_corners", f"Round sharp corner {i + 1} (r {r:.1f} mm)",
            "A rounded corner shrinks more evenly and is much less likely to lift off the bed (warping starts at sharp corners).",
            [to_part(piece)], param="chamfer_mm"))
    return out


def _foot_chamfer(oriented: Mesh, to_part, profile, size: float) -> list[dict]:
    """A small 45° chamfer along every bottom edge (outline and holes): the first layers are squashed
    and bulge out (elephant foot); the chamfer takes that material away, so edges stay straight and
    holes at the bed keep their size.

    Each outline loop is cut by ONE closed piece: the chamfer's cross-section swept along the loop,
    mitred at the corners. The sections sit just before and after each corner, never on it: a
    section through the part's own corner edge lets the two meshes touch edge on edge, and the
    boolean leaves zero-thickness fins and crumbs there (mesh errors in the slicer). Sharp corners
    break the sweep: there the pieces end square, reaching past outside corners and joined by a
    filler piece at inside corners."""
    from shapely.geometry.polygon import orient as orient_poly

    from .slicer import parts

    first = slice_mesh(oriented, profile.layer_height, max_layers=2)[0].geom
    if first.is_empty:
        return []
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
            ring_pieces = _ring_chamfer(c, section, size)
            points += len(c)
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


def _ring_chamfer(c: np.ndarray, section: np.ndarray, size: float) -> list[np.ndarray]:
    """Closed pieces cutting the chamfer along one outline loop ``c`` (material on the left). None
    when the sweep would fold (a short edge after a big turn): then this loop keeps its plain edge."""
    n = len(c)
    d = np.roll(c, -1, axis=0) - c
    d /= np.linalg.norm(d, axis=1, keepdims=True)  # edge k runs from c[k] to c[k + 1]
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

    if not sharp:
        rings = [r for k in range(n) for r in corner(k)]
        return [_sweep(rings, closed=True)] if forward(rings + rings[:1], [d[i // 2] for i in range(2 * n)]) else []
    pieces = []
    for a, b in zip(sharp, sharp[1:] + sharp[:1]):
        ks = [(a + i) % n for i in range((b - a) % n or n)]  # edges a … b-1, from sharp corner a to sharp corner b
        # Square ends: past outside corners (so the chamfer runs out cleanly), short of inside ones.
        start = c[a] - d[a] * ((size + 0.3) if convex[a] else -OFF_CORNER)
        end = c[b] + d[ks[-1]] * ((size + 0.3) if convex[b] else -OFF_CORNER)
        rings = [at(start, out[a])] + [r for k in ks[1:] for r in corner(k)] + [at(end, out[ks[-1]])]
        if not forward(rings, [d[a]] + [d[k] for k in ks[1:] for _ in range(2)]):
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


def _stress_chamfers(mesh: Mesh, weak_spot, h: float, size: float) -> list[dict]:
    """45° chamfers along the inside corner nearest the weak spot from the strength check (e.g. where a
    stem meets a plate): sharp inside corners concentrate stress, a chamfer spreads it out. Takes the
    whole connected corner (all inside edges linked to the ones near the weak spot)."""
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
    pieces = []
    for k in _connected(edges, [int(np.argmin(dist))])[:MAX_FILLET_PIECES]:
        e = edges[k]
        mid = (e["a"] + e["b"]) / 2
        half = (e["b"] - e["a"]) / 2
        inset = -(e["n1"] + e["n2"]) * 0.15  # reach a little into the part, so the union joins it
        prof = [mid + inset, mid + e["t1"] * size - e["n1"] * 0.15, mid + e["t2"] * size - e["n2"] * 0.15]
        pieces.append(_prism(prof, half))
    return [_edit("stress-chamfer", "stress_at_inside_corner", f"{size:.1f} mm 45° chamfer where the part is weakest",
                  "The strength check found the most stress at this sharp inside corner. A 45° chamfer spreads the load "
                  "over more material, often the cheapest way to make a part much stronger (a chamfer prints cleanly where a round fillet would be an overhang).",
                  pieces, op="add", param="stress_chamfer_mm")]


def concave_edges(mesh: Mesh, min_angle_deg: float = 30.0) -> list[dict]:
    """Sharp inside edges: two faces meeting at more than ``min_angle_deg`` with the second face in
    front of the first one's plane. Each: end points a, b; face normals n1, n2; t1, t2 the directions
    along each face away from the edge."""
    tris = mesh.triangles
    normals, _ = mesh.normals_and_areas()
    verts, inv = np.unique(np.round(tris.reshape(-1, 3), 4), axis=0, return_inverse=True)
    ids = inv.reshape(-1, 3)
    owner: dict = {}
    pairs = []
    for f, (i, j, k) in enumerate(ids):
        for a, b in ((i, j), (j, k), (k, i)):
            key = (min(a, b), max(a, b))
            if key in owner:
                pairs.append((key, owner.pop(key), f))
            else:
                owner[key] = f
    cos = np.cos(np.radians(min_angle_deg))
    centroids = tris.mean(axis=1)
    out = []
    for (a, b), f1, f2 in pairs:
        n1, n2 = normals[f1], normals[f2]
        if n1 @ n2 > cos:
            continue
        pa, pb = verts[a], verts[b]
        if (centroids[f2] - pa) @ n1 <= 1e-6:  # second face behind the first: an outside (convex) edge
            continue
        e = (pb - pa) / np.linalg.norm(pb - pa)
        t1, t2 = np.cross(n1, e), np.cross(n2, e)
        t1 = t1 if (centroids[f1] - pa) @ t1 > 0 else -t1
        t2 = t2 if (centroids[f2] - pa) @ t2 > 0 else -t2
        out.append({"a": pa, "b": pb, "va": int(a), "vb": int(b), "n1": n1, "n2": n2, "t1": t1, "t2": t2})
    return out


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
