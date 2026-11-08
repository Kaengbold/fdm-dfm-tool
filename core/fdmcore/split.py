"""Split a part that is too big for the printer into pieces that fit, with alignment pins (T-321).

The cut is flat, across one of the part's own axes (where CAD parts are usually flat), placed so
every piece fits the bed and, when a strength check ran, away from the weak spot. Each piece is the
part with everything beyond its cuts cut away: design edits (op "cut") that the app applies with
manifold-3d, or booleans.py locally. Matching holes on both faces of each cut take round pins,
printed separately (standing up, so they come out round) and glued in.
"""

from __future__ import annotations

import numpy as np
from shapely.geometry import Point

from .additions import extrude
from .features import _edit
from .mesh import Mesh
from .profile import load_data
from .slicer import section

PIN_D = 4.0  # mm: stiff enough to line the pieces up, small enough for thin walls
PIN_DEPTH = 6.0  # into each piece
PIN_WALL = 2.0  # plastic left round a pin hole
OVERSHOOT = 1.0  # cutting boxes reach this far past the part


def _fits(size, bed) -> bool:
    """Fits the bed in some turn on the bed or standing on any side (sizes sorted against each other)."""
    return bool(np.all(np.sort(size) <= np.sort(bed) + 1e-6))


def _frame(k: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Unit vectors (u, v, axis) with the cut across model axis ``k``."""
    e = np.eye(3)
    u, v = [e[i] for i in range(3) if i != k]
    return u, v, e[k]


def _box(lo, hi) -> np.ndarray:
    """Closed box (12 triangles, outward) from corner ``lo`` to ``hi``."""
    c = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], float)
    f = [[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6],
         [3, 0, 4], [3, 4, 7]]
    lo, hi = np.asarray(lo, float), np.asarray(hi, float)
    return (lo + c * (hi - lo))[f]


def _section(mesh: Mesh, k: int, at: float):
    """The part cut across axis ``k`` at ``at`` (2D, in u / v coordinates)."""
    u, v, a = _frame(k)
    return section(mesh.triangles @ np.stack([u, v, a]).T, at)


def _pin_spots(geom, gap: float) -> list[tuple[float, float]]:
    """Up to two pin centres inside the cut face, far apart (two pins also stop the pieces turning)."""
    inner = geom.buffer(-(PIN_D / 2 + gap / 2 + PIN_WALL)) if not geom.is_empty else geom
    if inner.is_empty:
        return []
    pts = [p for g in getattr(inner, "geoms", [inner]) for p in np.asarray(g.exterior.coords)[:-1]]
    if len(pts) < 2:
        return [tuple(inner.representative_point().coords[0])]
    pts = np.asarray(pts)
    d = np.linalg.norm(pts[:, None] - pts[None], axis=2)
    i, j = np.unravel_index(np.argmax(d), d.shape)
    if d[i, j] < PIN_D * 3:
        return [tuple(inner.representative_point().coords[0])]
    # Pull both a little inwards along the line between them: the eroded outline's corners are on its edge.
    a, b = pts[i], pts[j]
    a, b = a + (b - a) * 0.1, b + (a - b) * 0.1
    return [tuple(p) for p in (a, b) if inner.buffer(1e-6).contains(Point(p))] or [tuple(inner.representative_point().coords[0])]


def split_plan(mesh: Mesh, bed, weak_spot=None, fit_gap_mm: float | None = None) -> dict:
    """Where to cut and what each piece is. ``bed`` [x, y, z] mm; ``weak_spot`` (part coordinates)
    keeps cuts away from where the part is weakest. Returns axis, cut positions, each piece's edits
    and size, the pin (STL, count, sizes) and a plain explanation; raises if the part already fits
    or no flat cut across its axes makes the pieces fit."""
    pts = mesh.triangles.reshape(-1, 3)
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    size = hi - lo
    bed = np.asarray(bed, float)
    if _fits(size, bed):
        raise ValueError("The part already fits this printer: no need to split it")
    gap = fit_gap_mm if fit_gap_mm is not None else load_data("thresholds")["fits"]["gap_mm"]["snug"]
    best = None
    for k in np.argsort(-size):  # longest axis first
        for n in range(2, 7):
            length = size[k] / n
            piece = size.copy()
            piece[k] = length
            if not _fits(piece, bed):
                continue
            # Evenly spaced cuts, nudged (up to a quarter piece) away from the weak spot and onto one-piece sections.
            slack = 0.25 * length
            cuts = []
            for i in range(1, n):
                base = lo[k] + i * length
                options = base + np.linspace(-slack, slack, 9) * (1 if n == 2 else 0.5)
                scored = []
                for c in options:
                    geom = _section(mesh, k, c)
                    if geom.is_empty:
                        continue
                    parts = len(getattr(geom, "geoms", [geom]))
                    weak = 0.0
                    if weak_spot is not None:
                        weak = max(0.0, 1 - abs(float(np.asarray(weak_spot)[k]) - c) / max(length, 1e-9))
                    scored.append((parts > 1, weak, abs(c - base), float(c)))
                if not scored:
                    break
                cuts.append(min(scored)[3])
            if len(cuts) != n - 1:
                continue
            edges = [lo[k] - OVERSHOOT, *cuts, hi[k] + OVERSHOOT]
            if not all(_fits(np.where(np.arange(3) == k, b - a, size), bed) for a, b in zip(edges, edges[1:])):
                continue
            best = (int(k), cuts)
            break
        if best:
            break
    if best is None:
        raise ValueError("No straight cut across the part's sides makes the pieces fit this printer")
    k, cuts = best
    u, v, a = _frame(k)
    far_lo, far_hi = lo - OVERSHOOT, hi + OVERSHOOT
    holes, spots_per_cut = [], []
    for c in cuts:
        spots = _pin_spots(_section(mesh, k, c), gap)
        spots_per_cut.append(len(spots))
        for x, y in spots:
            cyl = extrude(Point(0, 0).buffer((PIN_D + gap) / 2, 32), -PIN_DEPTH, PIN_DEPTH)
            holes.append(x * u + y * v + c * a + cyl[..., :1] * u + cyl[..., 1:2] * v + cyl[..., 2:] * a)
    pieces = []
    edges = [None, *cuts, None]
    for i in range(len(cuts) + 1):
        cutters = []
        if edges[i] is not None:  # everything below this piece's lower cut
            top = np.where(np.arange(3) == k, edges[i], far_hi)
            cutters.append(_box(far_lo, top))
        if edges[i + 1] is not None:  # everything above its upper cut
            bottom = np.where(np.arange(3) == k, edges[i + 1], far_lo)
            cutters.append(_box(bottom, far_hi))
        edits = [_edit(f"split-{i}", "split", f"Piece {i + 1}", "Cut away the rest of the part.", cutters)]
        if holes:
            edits.append(_edit(f"split-pins-{i}", "split", "Pin holes", "Holes for the alignment pins.", holes))
        span = (edges[i] if edges[i] is not None else lo[k], edges[i + 1] if edges[i + 1] is not None else hi[k])
        pieces.append({"edits": edits, "size_mm": np.where(np.arange(3) == k, span[1] - span[0], size).round(1).tolist()})
    pin_len = 2 * PIN_DEPTH - 1.0  # a little shorter than both holes: glue has room and the faces meet
    pin = extrude(Point(0, 0).buffer(PIN_D / 2, 32), 0, pin_len)
    count = sum(spots_per_cut)
    name = "xyz"[k]
    text = (f"Cut {len(cuts)}× across its {name} side (at {', '.join(f'{c:.0f}' for c in cuts)} mm) into {len(pieces)} pieces "
            f"that each fit the bed. ")
    text += (f"Print {count} pins (Ø{PIN_D:g} × {pin_len:g} mm, standing up) and glue the pieces together on them; the "
             f"holes are Ø{PIN_D + gap:.2f} mm, {PIN_DEPTH:g} mm deep on each side." if count else
             "The cut face is too thin for pins: glue the pieces flat (super glue for PLA, or a solvent for ABS / ASA).")
    return {"axis": k, "cuts": [round(c, 2) for c in cuts], "pieces": pieces, "explanation": text,
            "pin": {"stl_triangles": pin.astype("<f4").tobytes(), "count": count, "diameter_mm": PIN_D, "length_mm": pin_len,
                    "hole_mm": round(PIN_D + gap, 2)}}
