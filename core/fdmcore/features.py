"""Feature recognition (holes, bosses) and design edits ("ghost" geometry).

Features are fitted on curved regions from regions.py: a cylinder's normals are
all perpendicular to its axis, so the axis is the direction the normals avoid
(smallest eigenvector of Σ n nᵀ). Radius comes from a circle fit of the points
projected onto the plane across the axis.

Design edits are returned as *cutter* meshes in part coordinates; the web app
subtracts them with manifold-3d (WASM) and reloads the result.
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


def design_edits(mesh: Mesh, region: np.ndarray, up, profile) -> list[dict]:
    """Suggested geometry changes for this build direction, with cutter meshes."""
    up = np.asarray(up, float) / np.linalg.norm(up)
    edits = []
    for f in detect_features(mesh, region):
        axis = np.asarray(f["axis"])
        if f["type"] == "hole" and f["full_circle"] and abs(axis @ up) < 0.2 and f["diameter"] >= TEARDROP_MIN_DIAMETER:
            edits.append({
                "id": f"teardrop-{f['region']}",
                "rule": "short_bridge",
                "title": f"Teardrop for the horizontal Ø{f['diameter']:.1f} mm hole",
                "description": "Makes the top of the hole a 45° point so it prints without sagging or support.",
                "cutter": _teardrop_cutter(f, up).astype("<f4").tobytes(),
            })
    edits += _corner_chamfers(mesh, up, profile)
    return edits


def _teardrop_cutter(f: dict, up: np.ndarray) -> np.ndarray:
    """Kite-shaped prism (centre, two 45° tangent points, apex) along the hole axis."""
    axis, c, r = np.asarray(f["axis"]), np.asarray(f["center"]), f["radius"]
    u = up - (up @ axis) * axis
    u /= np.linalg.norm(u)
    s = np.cross(axis, u)
    k = r / np.sqrt(2)
    profile = [c - 0.5 * r * u, c + k * s + k * u, c + r * np.sqrt(2) * u, c - k * s + k * u]
    return _prism(profile, axis * f["length"] / 2)


def _corner_chamfers(mesh: Mesh, up: np.ndarray, profile, size: float = 3.0) -> list[dict]:
    """Vertical chamfers on sharp footprint corners (where warping starts)."""
    from .checks import _sharp_corners

    r = rotation_to_z(up)
    oriented = orient(mesh, r)
    layers = slice_mesh(oriented, profile.layer_height, max_layers=60)
    corners = _sharp_corners(layers[0].geom)
    if not corners or profile.material["warp_risk"] == "low":
        return []
    # Undo orient(): print = R @ part + shift.
    pts = mesh.triangles.reshape(-1, 3) @ r.T
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    shift = np.array([-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]])
    out = []
    for i, (x, y) in enumerate(corners):
        top = _corner_height(layers, np.array([x, y]))
        e1, e2 = _corner_edges(layers[0].geom, np.array([x, y]))
        if e1 is None:
            continue
        p = np.array([x, y])
        tri = [p + (e1 + e2) * 0.0 - (e1 + e2) * 0.5, p + e1 * size, p + e2 * size]  # extends past the corner tip
        print_tris = _prism([np.array([*q, top / 2]) for q in tri], np.array([0, 0, top / 2 + 0.5]))
        part_tris = (print_tris.reshape(-1, 3) - shift) @ r  # inverse rotation: r.T @ v
        out.append({
            "id": f"chamfer-{i}",
            "rule": "sharp_bed_corners",
            "title": f"Chamfer sharp corner {i + 1} ({size:.0f} mm)",
            "description": "A cut-off corner shrinks less and is much less likely to lift off the bed.",
            "cutter": part_tris.reshape(-1, 3, 3).astype("<f4").tobytes(),
        })
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
    """Unit vectors along the two outline edges leaving corner ``p``."""
    from shapely.geometry.polygon import orient as orient_poly

    from .slicer import parts

    for poly in parts(geom):
        c = np.asarray(orient_poly(poly.simplify(0.05)).exterior.coords)[:-1]
        d = np.linalg.norm(c - p, axis=1)
        i = int(np.argmin(d))
        if d[i] < 1e-3:
            e1, e2 = c[i - 1] - c[i], c[(i + 1) % len(c)] - c[i]
            return e1 / np.linalg.norm(e1), e2 / np.linalg.norm(e2)
    return None, None


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
