"""Sideways (horizontal) holes in an oriented part.

Layer slices only see the *roof* of a sideways hole as a bridge. To get the hole
itself (diameter, position) we cut the part with vertical planes across the hole
axis: there the hole is a round inner ring. Candidate axes come from the part
itself: the walls of a sideways cylinder have normals that are partly vertical and
all perpendicular to the axis.
"""

from __future__ import annotations

import numpy as np

from .mesh import Mesh
from .slicer import parts, section

PLANES = 14  # cuts per direction
MAX_AXES = 4
ROUND_TOL = 0.06  # max radial deviation / radius for "round" (a 16-gon is ~2%, a teardrop tip ~40%)


def horizontal_holes(mesh: Mesh, min_d: float = 0.5, max_d: float = 40.0) -> list[dict]:
    """Round sideways holes: centre (print frame), axis, diameter, length along the axis, bbox."""
    out: list[dict] = []
    for angle in _axis_angles(mesh):
        d = np.array([np.cos(angle), np.sin(angle), 0.0])
        u = np.array([-d[1], d[0], 0.0])
        frame = np.stack([u, [0.0, 0.0, 1.0], d])  # rows: in-plane x, in-plane y (= up), cut direction
        tri = mesh.triangles @ frame.T
        t = tri[..., 2]
        lo, hi = float(t.min()), float(t.max())
        step = (hi - lo) / PLANES
        tmin, tmax = t.min(axis=1), t.max(axis=1)

        def rings_at(h: float) -> list[tuple[float, float, float]]:
            g = section(tri[(tmin < h) & (tmax > h)], float(h))
            return [c for poly in parts(g) for ring in poly.interiors if (c := _round_ring(np.asarray(ring.coords)[:-1, :2]))]

        found = [(*c, h) for h in lo + step * (np.arange(PLANES) + 0.5) + 1.1e-4
                 for c in rings_at(h) if min_d <= 2 * c[2] <= max_d]
        out += [h for h in _merge(found, frame, step, lo, hi) if _is_cylinder(h, rings_at)]
    return out


def _is_cylinder(hole: dict, rings_at) -> bool:
    """A sphere or cone also cuts as a circle, but its radius changes along the axis: cut again a
    bit to each side of the first and last cut. No ring there is fine (the hole ends, e.g. a thin plate)."""
    r = hole["diameter"] / 2
    if max(hole["radii"]) - min(hole["radii"]) > 0.04 * r:
        return False
    for h in (hole["h0"] - 0.4 * r, hole["h0"] + 0.4 * r, hole["h1"] - 0.4 * r, hole["h1"] + 0.4 * r):
        for cx, cy, r2 in rings_at(h):
            if abs(cx - hole["cx"]) < 0.3 * r and abs(cy - hole["cy"]) < 0.3 * r and abs(r2 - r) > 0.04 * r:
                return False
    return True


def _axis_angles(mesh: Mesh) -> list[float]:
    """Horizontal directions (radians, mod π) that sideways cylinders in the part run along."""
    n, a = mesh.normals_and_areas()
    slanted = (np.abs(n[:, 2]) > 0.15) & (np.abs(n[:, 2]) < 0.97)
    if not slanted.any():
        return []
    # Axis is perpendicular to the normal's horizontal part.
    ang = np.mod(np.arctan2(n[slanted, 1], n[slanted, 0]) + np.pi / 2, np.pi)
    hist, edges = np.histogram(ang, bins=36, range=(0, np.pi), weights=a[slanted])
    picks = []
    for i in np.argsort(-hist):
        if hist[i] < 0.1 * hist.max() or len(picks) == MAX_AXES:
            break
        if all(min(abs(i - j), 36 - abs(i - j)) > 1 for j in picks):
            picks.append(i)
    out = []
    for i in picks:
        sel = (ang >= edges[i]) & (ang < edges[i + 1])
        out.append(float(np.average(ang[sel], weights=a[slanted][sel])))
    return out


def _round_ring(pts: np.ndarray) -> tuple[float, float, float] | None:
    """(cx, cy, r) if the ring is a circle (least-squares fit within ROUND_TOL)."""
    if len(pts) < 8:
        return None
    A = np.column_stack([pts, np.ones(len(pts))])
    sol, *_ = np.linalg.lstsq(A, -(pts**2).sum(axis=1), rcond=None)
    cx, cy = -sol[0] / 2, -sol[1] / 2
    r2 = cx**2 + cy**2 - sol[2]
    if r2 <= 0:
        return None
    r = float(np.sqrt(r2))
    dev = np.abs(np.linalg.norm(pts - [cx, cy], axis=1) - r)
    return (float(cx), float(cy), r) if dev.max() <= ROUND_TOL * r else None


def _merge(found: list[tuple], frame: np.ndarray, step: float, lo: float, hi: float) -> list[dict]:
    """One hole per (centre, size) seen on the cuts. Gaps are allowed: a through hole crossed by
    other holes or pockets is still one hole to drill or fit a pin into."""
    holes: list[dict] = []
    for cx, cy, r, h in sorted(found, key=lambda f: f[3]):
        for hole in holes:
            if abs(hole["cx"] - cx) < 0.3 * r and abs(hole["cy"] - cy) < 0.3 * r and abs(hole["r"] - r) < 0.2 * r:
                hole["h1"] = h
                hole["rs"].append(r)
                break
        else:
            holes.append({"cx": cx, "cy": cy, "r": r, "h0": h, "h1": h, "rs": [r]})
    out = []
    u, z, d = frame
    for hole in holes:
        r = float(np.median(hole["rs"]))
        h0, h1 = hole["h0"] - step / 2, hole["h1"] + step / 2
        mid = hole["cx"] * u + hole["cy"] * z + (h0 + h1) / 2 * d
        ends = [hole["cx"] * u + hole["cy"] * z + h * d for h in (h0, h1)]
        box_lo = np.minimum(*ends) - r * np.abs(u) - r * z
        box_hi = np.maximum(*ends) + r * np.abs(u) + r * z
        out.append({
            "cx": hole["cx"], "cy": hole["cy"], "h0": hole["h0"], "h1": hole["h1"], "radii": hole["rs"],  # cut frame
            "center": mid.tolist(),
            "axis": d.tolist(),
            "diameter": 2 * r,
            "length": float(h1 - h0),
            "bbox": [box_lo.tolist(), box_hi.tolist()],
            # Seen on the first and last cut: open at both ends (a blind hole stops inside the part).
            "through": bool(hole["h0"] - lo < step and hi - hole["h1"] < step),
        })
    return out
