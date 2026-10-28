"""Placement on the printer bed: does the part fit, and how to turn it so it does.

The full print rotation is ``Rz @ rotation_to_z(up)``: first the build direction,
then (only if needed to fit) a turn about Z that lines the part's footprint up
with the bed's long axis.
"""

from __future__ import annotations

import numpy as np
import shapely

from .mesh import Mesh
from .orientation import rotation_to_z


def print_rotation(mesh: Mesh, up, bed) -> tuple[np.ndarray, dict]:
    """Rotation into print orientation and a fit report for a bed ``[x, y, z]`` (mm)."""
    r0 = rotation_to_z(up)
    pts = np.unique(np.round(mesh.triangles.reshape(-1, 3) @ r0.T, 3), axis=0)
    size = pts.max(axis=0) - pts.min(axis=0)
    bx, by, bz = bed
    fit = {"fits": True, "turned_deg": 0.0, "size_mm": size.tolist(), "bed_mm": list(bed)}
    if size[2] > bz:
        fit.update(fits=False, reason=f"{size[2]:.0f} mm tall, the printer only fits {bz:.0f} mm")
        return r0, fit
    if size[0] <= bx and size[1] <= by:
        return r0, fit

    # Smallest footprint rectangle, long side along the bed's longer axis.
    rect = shapely.multipoints(pts[:, :2]).convex_hull.minimum_rotated_rectangle
    c = np.asarray(rect.exterior.coords)
    e1, e2 = c[1] - c[0], c[2] - c[1]
    long_edge = e1 if np.linalg.norm(e1) >= np.linalg.norm(e2) else e2
    a, b = max(np.linalg.norm(e1), np.linalg.norm(e2)), min(np.linalg.norm(e1), np.linalg.norm(e2))
    target = 0.0 if bx >= by else np.pi / 2
    turn = target - np.arctan2(long_edge[1], long_edge[0])
    if a <= max(bx, by) and b <= min(bx, by):
        rz = np.array([[np.cos(turn), -np.sin(turn), 0], [np.sin(turn), np.cos(turn), 0], [0, 0, 1]])
        fit.update(turned_deg=float(np.degrees(turn) % 360), size_mm=[a, b, size[2]] if bx >= by else [b, a, size[2]])
        return rz @ r0, fit
    fit.update(fits=False, reason=f"footprint {a:.0f} × {b:.0f} mm, the bed is {bx:.0f} × {by:.0f} mm")
    return r0, fit
