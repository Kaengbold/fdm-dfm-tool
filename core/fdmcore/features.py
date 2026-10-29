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
from .slicer import slice_mesh

TEARDROP_MIN_DIAMETER = 5.0  # smaller horizontal holes bridge fine


def detect_features(mesh: Mesh, region: np.ndarray) -> list[dict]:
    normals, areas = mesh.normals_and_areas()
    centroids = mesh.triangles.mean(axis=1)
    out = []
    for r in np.unique(region):
        sel = region == r
        if sel.sum() < 6:
            continue
        n, a, c = normals[sel], areas[sel], centroids[sel]
        if np.linalg.norm((n * a[:, None]).sum(axis=0)) / a.sum() > 0.99:
            continue  # flat
        feat = _fit_cylinder(n, a, c, mesh.triangles[sel])
        if feat:
            feat["region"] = int(r)
            out.append(feat)
    return out


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
    }


# Adjustable edit sizes: default, min, max (the UI shows these as inputs).
PARAMS = {
    "chamfer_mm": (3.0, 0.5, 10.0),
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


def design_edits(mesh: Mesh, region: np.ndarray, up, profile, params: dict | None = None) -> list[dict]:
    """Suggested geometry changes for this build direction, as closed meshes in part coordinates."""
    from .additions import ledge_chamfers, sacrificial_layers, thicken_walls

    up = np.asarray(up, float) / np.linalg.norm(up)
    prm = edit_params(params)
    angle = prm["teardrop_angle_deg"]
    edits = []
    for f in detect_features(mesh, region):
        axis = np.asarray(f["axis"])
        if f["type"] == "hole" and f["full_circle"] and abs(axis @ up) < 0.2 and f["diameter"] >= TEARDROP_MIN_DIAMETER:
            edits.append(_edit(
                f"teardrop-{f['region']}", "short_bridge", f"Teardrop for the horizontal Ø{f['diameter']:.1f} mm hole",
                f"Makes the top of the hole a {angle:.0f}° point so it prints without sagging or support.",
                [_teardrop_cutter(f, up, angle)], param="teardrop_angle_deg"))
    r = rotation_to_z(up)
    oriented = orient(mesh, r)
    to_part = _to_part(mesh, r)
    edits += _corner_chamfers(oriented, to_part, profile, prm["chamfer_mm"])
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


def _to_part(mesh: Mesh, r: np.ndarray):
    """Map triangles from the oriented part (orient(mesh, r)) back to part coordinates."""
    pts = mesh.triangles.reshape(-1, 3) @ r.T
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    shift = np.array([-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]])
    return lambda tris: ((np.asarray(tris).reshape(-1, 3) - shift) @ r).reshape(-1, 3, 3)  # r.T @ v per point


def _teardrop_cutter(f: dict, up: np.ndarray, angle_deg: float = 45.0) -> np.ndarray:
    """Kite-shaped prism (centre, two tangent points, apex) along the hole axis. The roof
    lines are ``angle_deg`` from vertical: they touch the circle at r·(sin θ up ± cos θ side)
    and meet at r / sin θ above the centre."""
    axis, c, r = np.asarray(f["axis"]), np.asarray(f["center"]), f["radius"]
    u = up - (up @ axis) * axis
    u /= np.linalg.norm(u)
    s = np.cross(axis, u)
    th = np.radians(angle_deg)
    up_k, side_k = r * np.sin(th), r * np.cos(th)
    profile = [c - 0.5 * r * u, c + side_k * s + up_k * u, c + r / np.sin(th) * u, c - side_k * s + up_k * u]
    return _prism(profile, axis * f["length"] / 2)


def _corner_chamfers(oriented: Mesh, to_part, profile, size: float = 3.0) -> list[dict]:
    """Vertical chamfers on sharp footprint corners (where warping starts)."""
    from .checks import _sharp_corners

    layers = slice_mesh(oriented, profile.layer_height, max_layers=60)
    corners = _sharp_corners(layers[0].geom)
    if not corners or profile.material["warp_risk"] == "low":
        return []
    out = []
    for i, (x, y) in enumerate(corners):
        top = _corner_height(layers, np.array([x, y]))
        e1, e2, edge = _corner_edges(layers[0].geom, np.array([x, y]))
        if e1 is None:
            continue
        # Two chamfers on a short edge must not meet, or they leave a new sharp point between them.
        cut = min(size, 0.4 * edge)
        p = np.array([x, y])
        tri = [p - (e1 + e2) * 0.5, p + e1 * cut, p + e2 * cut]  # extends past the corner tip
        print_tris = _prism([np.array([*q, top / 2]) for q in tri], np.array([0, 0, top / 2 + 0.5]))
        out.append(_edit(
            f"chamfer-{i}", "sharp_bed_corners", f"Chamfer sharp corner {i + 1} ({cut:.1f} mm)",
            "A cut-off corner shrinks less and is much less likely to lift off the bed.",
            [to_part(print_tris)], param="chamfer_mm"))
    return out


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
