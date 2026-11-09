"""Split a part that is too big for the printer into pieces that fit, with alignment pins (T-321).

The cut is flat, across one of the part's own axes (where CAD parts are usually flat), placed so
every piece fits the bed and, when a strength check ran, away from the weak spot. Each piece is the
part with everything beyond its cuts cut away: design edits (op "cut") that the app applies with
manifold-3d, or booleans.py locally. The pieces are joined with round pins in matching holes on both
faces of each cut (printed separately, standing up, so they come out round, and glued in), or with a
sliding dovetail: the piece below each cut keeps a flared tongue of its own plastic, the piece above
gets the matching groove. Both are flat outlines pushed straight across the part, so cutting away
is all it takes. Each piece also says how to turn it to print (lying low).
"""

from __future__ import annotations

import itertools

import numpy as np
from shapely.geometry import Point, Polygon, box

from .additions import extrude
from .features import _edit
from .mesh import Mesh
from .profile import load_data
from .slicer import section

PIN_D = 4.0  # mm: stiff enough to line the pieces up, small enough for thin walls
PIN_DEPTH = 6.0  # into each piece
PIN_WALL = 2.0  # plastic left round a pin hole
OVERSHOOT = 1.0  # cutting boxes reach this far past the part
DOVETAIL_DEG = 15.0  # flare of each side from straight: holds the pieces together, prints without support
DOVETAIL_WALL = 3.0  # plastic left on each side of the groove
DOVETAIL_MAX_W = 24.0  # widest part of the tongue (mm)
DOVETAIL_MIN_W = 8.0  # narrower than this, a dovetail is too weak: pins instead


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


def _place(tris: np.ndarray, x, y, z) -> np.ndarray:
    """Local triangles (x, y, z columns) laid along the axes ``x``, ``y``, ``z``, still wound outward
    (a mirrored set of axes would turn the solid inside out: a hole would then add plastic)."""
    out = tris[..., :1] * np.asarray(x, float) + tris[..., 1:2] * np.asarray(y, float) + tris[..., 2:] * np.asarray(z, float)
    return out[:, ::-1] if np.linalg.det(np.stack([x, y, z]).astype(float)) < 0 else out


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


def _dovetail(geom, k: int):
    """A sliding dovetail for the cut face ``geom`` (2D in the u / v of ``_frame(k)``): it runs along the
    face's shorter side (a short slide to put together) and sits in the middle of the longer one.
    Returns (run axis, across axis, centre across, widest W, narrowest w, height h) or None when the face
    hasn't room for one."""
    if geom.is_empty:
        return None
    u, v, _ = _frame(k)
    u0, v0, u1, v1 = geom.bounds
    run, across, (r0, r1), (a0, a1) = (u, v, (u0, u1), (v0, v1)) if u1 - u0 <= v1 - v0 else (v, u, (v0, v1), (u0, u1))
    width = min(0.6 * (a1 - a0 - 2 * DOVETAIL_WALL), DOVETAIL_MAX_W)
    if width < DOVETAIL_MIN_W:
        return None
    h = float(np.clip(width / 2, 3.0, 8.0))
    narrow = width - 2 * h * np.tan(np.radians(DOVETAIL_DEG))
    mid = (a0 + a1) / 2
    lo, hi = mid - width / 2 - DOVETAIL_WALL, mid + width / 2 + DOVETAIL_WALL
    strip = box(r0, lo, r1, hi) if run is u else box(lo, r0, hi, r1)
    if geom.intersection(strip).area < 0.6 * strip.area:  # mostly empty there (a ring, an L): no hold
        return None
    return run, across, mid, width, narrow, h


def _dovetail_cutters(d, c: float, a, gap: float, far_lo, far_hi) -> tuple[np.ndarray, np.ndarray]:
    """The two cutting solids for a dovetail at cut height ``c`` along axis ``a``: (for the piece below:
    everything above the cut except the tongue, for the piece above: everything below plus the groove,
    ``gap`` wider)."""
    run, across, mid, width, narrow, h = d
    al, ah = float(far_lo @ across), float(far_hi @ across)
    zl, zh = float(far_lo @ a), float(far_hi @ a)
    rl, rh = float(far_lo @ run), float(far_hi @ run)
    tongue = Polygon([(mid - narrow / 2, c - 1.0), (mid + narrow / 2, c - 1.0), (mid + narrow / 2, c), (mid + width / 2, c + h),
                      (mid - width / 2, c + h), (mid - narrow / 2, c)])
    above = box(al, c, ah, zh).difference(tongue)
    below = box(al, zl, ah, c).union(tongue.buffer(gap / 2, join_style=2))
    return (_place(extrude(above, rl, rh), across, a, run), _place(extrude(below, rl, rh), across, a, run))


ROTATIONS = [np.array(m, float) for p in itertools.permutations(range(3)) for s in itertools.product((1, -1), repeat=3)
             if np.isclose(np.linalg.det(m := np.eye(3)[list(p)] * np.array(s)[:, None]), 1)]


def _print_turn(size, bed, run=None) -> list[list[float]]:
    """How to turn a piece (model sizes ``size``) to print it: fitting the bed and lying as low as it goes
    (steady, quick); of equally low ways, the one with the dovetail's ``run`` axis upright, so its flared
    sides are printed as straight walls."""
    best = None
    for r in ROTATIONS:
        turned = np.abs(r @ size)
        if np.all(turned <= np.asarray(bed, float) + 1e-6):
            key = (round(float(turned[2]), 1), run is None or abs(r[2] @ run) < 0.5)
            if best is None or key < best[0]:
                best = (key, r)
    return (best[1] if best is not None else np.eye(3)).tolist()


def split_plan(mesh: Mesh, bed, weak_spot=None, fit_gap_mm: float | None = None, joint: str = "pins") -> dict:
    """Where to cut and what each piece is. ``bed`` [x, y, z] mm; ``weak_spot`` (part coordinates)
    keeps cuts away from where the part is weakest; ``joint`` "pins" or "dovetail" (pins where a cut
    face has no room for one). Returns axis, cut positions, ``joint``, each piece's edits, size and print
    ``rotation`` (part → printer, as checks.orient), the pin (STL, count, sizes) and a plain explanation;
    raises if the part already fits or no flat cut across its axes makes the pieces fit."""
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
    edges = [None, *cuts, None]
    spans = [(edges[i] if edges[i] is not None else lo[k], edges[i + 1] if edges[i + 1] is not None else hi[k]) for i in range(len(cuts) + 1)]
    note = ""
    tails = [_dovetail(_section(mesh, k, c), k) for c in cuts] if joint == "dovetail" else []
    if tails and not all(tails):
        note, tails = "A cut face has no room for a dovetail, so the pieces get pins. ", []
    if tails:  # each piece below a cut is longer by its tongue: it must still fit
        grown = [np.where(np.arange(3) == k, s1 - s0 + (tails[i][5] if i < len(cuts) else 0), size) for i, (s0, s1) in enumerate(spans)]
        if not all(_fits(g, bed) for g in grown):
            note, tails = "With a dovetail the pieces would no longer fit the bed, so they get pins. ", []
    holes, spots_per_cut = [], []
    for c in [] if tails else cuts:
        spots = _pin_spots(_section(mesh, k, c), gap)
        spots_per_cut.append(len(spots))
        for x, y in spots:
            cyl = extrude(Point(0, 0).buffer((PIN_D + gap) / 2, 32), -PIN_DEPTH, PIN_DEPTH)
            holes.append(x * u + y * v + c * a + _place(cyl, u, v, a))
    pieces = []
    for i, (s0, s1) in enumerate(spans):
        cutters = []
        if edges[i] is not None:  # everything below this piece's lower cut (and the groove for the tongue below)
            top = np.where(np.arange(3) == k, edges[i], far_hi)
            cutters.append(_dovetail_cutters(tails[i - 1], edges[i], a, gap, far_lo, far_hi)[1] if tails else _box(far_lo, top))
        if edges[i + 1] is not None:  # everything above its upper cut (but its tongue)
            bottom = np.where(np.arange(3) == k, edges[i + 1], far_lo)
            cutters.append(_dovetail_cutters(tails[i], edges[i + 1], a, gap, far_lo, far_hi)[0] if tails else _box(bottom, far_hi))
        edits = [_edit(f"split-{i}", "split", f"Piece {i + 1}", "Cut away the rest of the part.", cutters)]
        if holes:
            edits.append(_edit(f"split-pins-{i}", "split", "Pin holes", "Holes for the alignment pins.", holes))
        length = s1 - s0 + (tails[i][5] if tails and i < len(cuts) else 0.0)
        piece_size = np.where(np.arange(3) == k, length, size)
        pieces.append({"edits": edits, "size_mm": piece_size.round(1).tolist(), "rotation": _print_turn(piece_size, bed, tails[0][0] if tails else None)})
    pin_len = 2 * PIN_DEPTH - 1.0  # a little shorter than both holes: glue has room and the faces meet
    pin = extrude(Point(0, 0).buffer(PIN_D / 2, 32), 0, pin_len)
    count = sum(spots_per_cut)
    name = "xyz"[k]
    text = note + (f"Cut {len(cuts)}× across its {name} side (at {', '.join(f'{c:.0f}' for c in cuts)} mm) into {len(pieces)} "
                   f"pieces that each fit the bed. ")
    if tails:
        slide = "xyz"[int(np.argmax(np.abs(tails[0][0])))]
        text += (f"Each cut has a dovetail: slide the pieces together along their {slide} side and glue them (the groove is "
                 f"{gap:.2f} mm wider than the tongue).")
    else:
        text += (f"Print {count} pins (Ø{PIN_D:g} × {pin_len:g} mm, standing up) and glue the pieces together on them; the "
                 f"holes are Ø{PIN_D + gap:.2f} mm, {PIN_DEPTH:g} mm deep on each side." if count else
                 "The cut face is too thin for pins: glue the pieces flat (super glue for PLA, or a solvent for ABS / ASA).")
    return {"axis": k, "cuts": [round(c, 2) for c in cuts], "joint": "dovetail" if tails else "pins", "pieces": pieces, "explanation": text,
            "pin": {"stl_triangles": pin.astype("<f4").tobytes(), "count": count, "diameter_mm": PIN_D, "length_mm": pin_len,
                    "hole_mm": round(PIN_D + gap, 2)}}
