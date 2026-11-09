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


# A bed slinger shakes the part along Y: a tall thin part standing broadside to that motion wobbles like a
# diving board (Z banding, coming loose) [sourced_F mistake #30, Prusa KB tall and thin objects].
SLINGER_MIN_HEIGHT_MM = 50.0
SLINGER_SLENDER = 5.0  # height at least this × the footprint's thin side


def _slender(pts: np.ndarray, size: np.ndarray) -> bool:
    """Tall and thin: high, with a narrow footprint (its smallest rotated rectangle's short side)."""
    if size[2] < SLINGER_MIN_HEIGHT_MM:
        return False
    rect = shapely.multipoints(pts[:, :2]).convex_hull.minimum_rotated_rectangle
    c = np.asarray(rect.exterior.coords)
    thin = min(np.linalg.norm(c[1] - c[0]), np.linalg.norm(c[2] - c[1]))
    return size[2] >= SLINGER_SLENDER * max(thin, 1e-6)


PLATE_GAP_MM = 10.0  # between copies: room for the nozzle and a brim, and less stringing between them
PLATE_EDGE_MM = 5.0  # kept clear round the bed's edge
MAX_COPIES = 100


def plate_layout(footprint, bed, count: int | None = None, gap: float = PLATE_GAP_MM) -> tuple[int, list[list[float]]]:
    """How many copies of a part with this ``footprint`` (x, y mm, as it prints) fit the ``bed`` in rows,
    ``gap`` apart, and where ``count`` of them go (offsets from the bed's centre, first one in the middle row,
    rows filled from the front). (0, []) when not even one fits."""
    w, d = float(footprint[0]), float(footprint[1])
    bx, by = bed[0] - 2 * PLATE_EDGE_MM, bed[1] - 2 * PLATE_EDGE_MM
    cols, rows = int((bx + gap) // (w + gap)), int((by + gap) // (d + gap))
    most = min(cols * rows, MAX_COPIES) if w <= bed[0] and d <= bed[1] else 0
    if most == 0 and w <= bed[0] and d <= bed[1]:
        most, cols, rows = 1, 1, 1  # fits alone, not with the edge margin
    n = most if count is None else max(0, min(int(count), most))
    if n == 0:
        return most, []
    use_cols = min(cols, n)
    use_rows = -(-n // use_cols)
    out = []
    for k in range(n):
        r, c = divmod(k, use_cols)
        in_row = min(use_cols, n - r * use_cols)
        out.append([(c - (in_row - 1) / 2) * (w + gap), (r - (use_rows - 1) / 2) * (d + gap)])
    return most, out


def print_rotation(mesh: Mesh, up, bed, moving_bed: bool = False) -> tuple[np.ndarray, dict]:
    """Rotation into print orientation and a fit report for a bed ``[x, y, z]`` (mm). ``moving_bed``: the bed
    moves along Y; a tall thin part is turned so its footprint's long side runs along Y (``for_bed_motion``)."""
    r0 = rotation_to_z(up)
    pts = mesh.triangles.reshape(-1, 3) @ r0.T
    size = pts.max(axis=0) - pts.min(axis=0)
    bx, by, bz = bed
    fit = {"fits": True, "turned_deg": 0.0, "size_mm": size.tolist(), "bed_mm": list(bed)}
    if size[2] > bz:
        fit.update(fits=False, reason=f"{size[2]:.0f} mm tall, the printer only fits {bz:.0f} mm")
        return r0, fit
    if size[0] <= bx and size[1] <= by and not (moving_bed and _slender(pts, size)):
        return r0, fit

    # Smallest footprint rectangle, long side along the bed's longer axis.
    rect = shapely.multipoints(pts[:, :2]).convex_hull.minimum_rotated_rectangle
    c = np.asarray(rect.exterior.coords)
    e1, e2 = c[1] - c[0], c[2] - c[1]
    long_edge = e1 if np.linalg.norm(e1) >= np.linalg.norm(e2) else e2
    a, b = max(np.linalg.norm(e1), np.linalg.norm(e2)), min(np.linalg.norm(e1), np.linalg.norm(e2))
    fits_as_is = size[0] <= bx and size[1] <= by
    target = np.pi / 2 if fits_as_is or by > bx else 0.0  # along Y for the bed's motion (it fits), else the bed's long side
    turn = target - np.arctan2(long_edge[1], long_edge[0])
    if a <= max(bx, by) and b <= min(bx, by):
        rz = np.array([[np.cos(turn), -np.sin(turn), 0], [np.sin(turn), np.cos(turn), 0], [0, 0, 1]])
        fit.update(turned_deg=float(np.degrees(turn) % 360), size_mm=[b, a, size[2]] if target else [a, b, size[2]],
                   for_bed_motion=fits_as_is)
        return rz @ r0, fit
    if fits_as_is:  # turned along Y it wouldn't fit: leave it
        return r0, fit
    fit.update(fits=False, reason=f"footprint {a:.0f} × {b:.0f} mm, the bed is {bx:.0f} × {by:.0f} mm")
    return r0, fit
