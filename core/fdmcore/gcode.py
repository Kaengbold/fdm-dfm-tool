"""Read a sliced part's G-code: every extruded line with its kind, direction, plastic and time.

The detailed level of the simulation works from what the printer will really do instead of
estimates: which lines are walls, solid layers or sparse infill, which way each line runs (a
printed line is strong along itself and weak across), how much plastic sits where, and when
each spot was printed (for heat). Works with OrcaSlicer / Bambu Studio (``; FEATURE:``),
PrusaSlicer (``;TYPE:``) and Cura (``;TYPE:``) comments. Pure numpy, so it runs in the browser
on a G-code the user sliced themselves, and locally on one OrcaSlicer wrote for us.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np

from .voxel import VoxelGrid

# What each printed line is. The part itself is walls + solid + sparse.
WALL, SOLID, SPARSE, SUPPORT, OTHER = range(5)
KIND_NAMES = ("wall", "solid", "sparse", "support", "other")
PART_KINDS = (WALL, SOLID, SPARSE)
ARC_STEP_MM = 0.5  # arcs (G2/G3, arc fitting) are split into chords this long
MAX_SIZE_GAP_MM = 2.0  # G-code and model sizes may differ this much (elephant-foot and hole compensation)


@dataclass
class Toolpath:
    """Extruded lines in printer coordinates (z = top of the line, as the nozzle sees it)."""

    p0: np.ndarray  # (N, 3) start
    p1: np.ndarray  # (N, 3) end
    volume: np.ndarray  # (N,) mm³ of plastic
    kind: np.ndarray  # (N,) uint8, WALL / SOLID / SPARSE / SUPPORT / OTHER
    layer: np.ndarray  # (N,) layer index
    layer_height: np.ndarray  # (N,) mm
    t0: np.ndarray  # (N,) s since the start of the print, when the line starts
    t1: np.ndarray  # (N,) s, when it ends
    layer_z: np.ndarray  # (L,) nozzle height of each layer
    total_s: float  # whole print, slicer's estimate when the file has one
    slicer: str

    def part(self) -> np.ndarray:
        return np.isin(self.kind, PART_KINDS)


def kind_of(feature: str) -> int:
    """Line kind from a slicer's feature name (Orca "Sparse infill", Prusa "External perimeter", Cura "WALL-OUTER")."""
    f = feature.lower()
    if "support" in f:
        return SUPPORT
    if "wall" in f or "perimeter" in f:
        return WALL
    if any(k in f for k in ("solid", "top", "bottom", "bridge", "gap", "skin", "ironing")):
        return SOLID
    if "infill" in f or f == "fill":
        return SPARSE
    return OTHER


_FEATURE = re.compile(r"^;\s*(?:FEATURE|TYPE):\s*(.+?)\s*$")
_TIME = (re.compile(r";\s*model printing time:\s*([^;\n]+)"), re.compile(r";\s*total estimated time:\s*([^;\n]+)"),
         re.compile(r";\s*estimated printing time \(normal mode\)\s*=\s*([^\n]+)"), re.compile(r";TIME:(\d+)"))
_DIAMETER = re.compile(r";\s*filament_diameter\s*=\s*([\d.]+)")


def _seconds(text: str) -> float:
    if text.strip().isdigit():
        return float(text)
    parts = {u: float(v) for v, u in re.findall(r"([\d.]+)\s*([dhms])", text)}
    return parts.get("d", 0) * 86400 + parts.get("h", 0) * 3600 + parts.get("m", 0) * 60 + parts.get("s", 0)


def _words(line: str) -> dict[str, float]:
    out = {}
    for w in line.split(";", 1)[0].split()[1:]:
        try:
            out[w[0].upper()] = float(w[1:])
        except ValueError:
            pass
    return out


def parse(text: str) -> Toolpath:
    """Every extruding move as a straight line (arcs split into chords). Times come from the feed
    rates and are scaled to the slicer's own estimate when the file has one (it knows the
    printer's acceleration; we don't)."""
    m = _DIAMETER.search(text)
    fil_area = np.pi * (float(m.group(1)) if m else 1.75) ** 2 / 4
    slicer = "orca" if "; FEATURE:" in text else "prusa" if ";TYPE:" in text and "PrusaSlicer" in text else "cura" if ";TYPE:" in text else "unknown"

    x = y = z = 0.0
    e_abs, e_rel, xyz_rel = 0.0, False, False
    feed = 1800.0  # mm/min
    kind = OTHER
    t = 0.0
    rows = []  # (x0, y0, z0, x1, y1, z1, volume, kind, t0, t1)

    # Plain floats in the loop: a big print has millions of moves (and Pyodide is ~3× slower).
    for line in text.splitlines():
        if not line:
            continue
        c = line[0]
        if c == ";":
            if (fm := _FEATURE.match(line)) is not None:
                kind = kind_of(fm.group(1))
            continue
        if c not in "GM":
            continue
        cmd = line.split(None, 1)[0].split(";", 1)[0].upper()
        if cmd in ("G0", "G1", "G2", "G3"):
            w = _words(line)
            if "F" in w:
                feed = max(w["F"], 1.0)
            nx = (x + w["X"] if xyz_rel else w["X"]) if "X" in w else x
            ny = (y + w["Y"] if xyz_rel else w["Y"]) if "Y" in w else y
            nz = (z + w["Z"] if xyz_rel else w["Z"]) if "Z" in w else z
            de = 0.0
            if "E" in w:
                de = w["E"] if e_rel else w["E"] - e_abs
                e_abs = e_abs + w["E"] if e_rel else w["E"]
            if cmd in ("G2", "G3") and ("I" in w or "J" in w):
                pts = _arc(np.array([x, y, z]), np.array([nx, ny, nz]), w.get("I", 0.0), w.get("J", 0.0), cmd == "G2")
                lengths = np.linalg.norm(np.diff(pts, axis=0), axis=1)
                total = float(lengths.sum())
                dt = (total if total > 0 else abs(de)) / feed * 60
                if de > 0 and total > 1e-6:
                    ts = t + np.concatenate([[0], np.cumsum(lengths)]) / total * dt
                    for k in range(len(lengths)):
                        rows.append((*pts[k], *pts[k + 1], de * fil_area * lengths[k] / total, kind, ts[k], ts[k + 1]))
            else:
                total = ((nx - x) ** 2 + (ny - y) ** 2 + (nz - z) ** 2) ** 0.5
                dt = (total if total > 0 else abs(de)) / feed * 60
                if de > 0 and total > 1e-6:
                    rows.append((x, y, z, nx, ny, nz, de * fil_area, kind, t, t + dt))
            t += dt
            x, y, z = nx, ny, nz
        elif cmd == "G90":
            xyz_rel = False
        elif cmd == "G91":
            xyz_rel = True
        elif cmd == "M82":
            e_rel = False
        elif cmd == "M83":
            e_rel = True
        elif cmd == "G92":
            w = _words(line)
            if "E" in w:
                e_abs = w["E"]
            x, y, z = w.get("X", x), w.get("Y", y), w.get("Z", z)
        elif cmd == "G4":  # dwell
            w = _words(line)
            t += w.get("S", 0.0) + w.get("P", 0.0) / 1000

    if not rows:
        raise ValueError("No printed lines found: is this a G-code file from a slicer?")
    a = np.array(rows, dtype=float)
    p0, p1 = a[:, 0:3], a[:, 3:6]
    total_s = t
    for rx in _TIME:
        if (tm := rx.search(text)) is not None:
            total_s = _seconds(tm.group(1))
            break
    scale = total_s / t if t > 0 and total_s > 0 else 1.0

    # Layers: the heights the part's own lines are printed at (support may use its own layer heights);
    # every other line belongs to the part layer at or above it.
    kinds = a[:, 7].astype(np.uint8)
    z = np.round(p1[:, 2], 3)
    part = np.isin(kinds, PART_KINDS)
    layer_z = np.unique(z[part]) if part.any() else np.unique(z)
    layer = np.minimum(np.searchsorted(layer_z, z - 1e-3), len(layer_z) - 1)
    below = np.concatenate([[0.0], layer_z[:-1]])
    lh = (layer_z - below)[layer]
    if not part.all():  # support / skirt lines: their own height step, at most the part's
        own = np.diff(np.concatenate([[0.0], np.unique(z)]))[np.searchsorted(np.unique(z), z)]
        lh = np.where(part, lh, np.minimum(own, np.median(lh[part]) if part.any() else own))
    return Toolpath(p0=p0, p1=p1, volume=a[:, 6], kind=kinds, layer=layer, layer_height=lh,
                    t0=a[:, 8] * scale, t1=a[:, 9] * scale, layer_z=layer_z, total_s=float(total_s), slicer=slicer)


def _arc(p0: np.ndarray, p1: np.ndarray, i: float, j: float, clockwise: bool) -> np.ndarray:
    """Points along a G2/G3 arc (centre p0 + (i, j)); same start and end = full circle."""
    c = p0[:2] + (i, j)
    r = np.hypot(i, j)
    a0 = np.arctan2(p0[1] - c[1], p0[0] - c[0])
    a1 = np.arctan2(p1[1] - c[1], p1[0] - c[0])
    sweep = a1 - a0
    if clockwise:
        sweep = sweep - 2 * np.pi if sweep >= -1e-9 else sweep
    else:
        sweep = sweep + 2 * np.pi if sweep <= 1e-9 else sweep
    n = max(int(np.ceil(abs(sweep) * r / ARC_STEP_MM)), 1)
    a = a0 + sweep * np.linspace(0, 1, n + 1)
    pts = np.column_stack([c[0] + r * np.cos(a), c[1] + r * np.sin(a), np.linspace(p0[2], p1[2], n + 1)])
    pts[-1] = p1
    return pts


def layer_times(tp: Toolpath) -> dict:
    """Per part layer: its height, layer height, the part's plastic in it (mm³) and the time from its
    start to the next layer's start (s) — how long it cools before the next one goes on. Support
    printed in between counts as cooling time, as it does on the printer."""
    n = len(tp.layer_z)
    start = np.full(n, np.inf)
    np.minimum.at(start, tp.layer, tp.t0)
    end = np.zeros(n)
    np.maximum.at(end, tp.layer, tp.t1)
    start = np.minimum.accumulate(start[::-1])[::-1]  # a layer with nothing of its own starts with the next
    seconds = np.append(np.diff(start), end[-1] - start[-1])
    part = tp.part()
    volume = np.bincount(tp.layer[part], tp.volume[part], minlength=n)
    below = np.concatenate([[0.0], tp.layer_z[:-1]])
    return {"z": tp.layer_z, "layer_height": tp.layer_z - below, "volume": volume, "seconds": np.maximum(seconds, 1e-3)}


def bead_centres(tp: Toolpath) -> tuple[np.ndarray, np.ndarray]:
    """Line start/end at the middle of the bead's height (the nozzle is at its top). Both ends at the
    end's height: a line that starts right after a z-hop or layer change still lies in its layer."""
    a, b = tp.p0.copy(), tp.p1.copy()
    a[:, 2] = b[:, 2] = tp.p1[:, 2] - tp.layer_height / 2
    return a, b


def samples(tp: Toolpath, spacing: float, which: np.ndarray | None = None) -> dict:
    """Points every ``spacing`` mm along the lines, each with its share of the plastic, the line's
    direction, kind and time (lines shorter than ``spacing`` give one point)."""
    which = tp.part() if which is None else which
    a, b = bead_centres(tp)
    a, b = a[which], b[which]
    d = b - a
    length = np.linalg.norm(d, axis=1)
    n = np.maximum(np.ceil(length / spacing).astype(int), 1)
    seg = np.repeat(np.arange(len(a)), n)
    k = np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n)
    f = (k + 0.5) / n[seg]
    idx = np.flatnonzero(which)[seg]
    return {
        "points": a[seg] + d[seg] * f[:, None],
        "dirs": d[seg] / np.maximum(length[seg], 1e-12)[:, None],
        "volume": tp.volume[idx] / n[seg],
        "kind": tp.kind[idx],
        "time": tp.t0[idx] + (tp.t1[idx] - tp.t0[idx]) * f,
        "layer": tp.layer[idx],
    }


def placement(tp: Toolpath, oriented_lo: np.ndarray, oriented_hi: np.ndarray, inside) -> tuple[np.ndarray, np.ndarray]:
    """Where the G-code's part sits relative to our oriented model: ``(q, shift)`` with
    oriented = q @ printer + shift. Slicers move the part to the bed centre and may turn it about
    the vertical, so the bounding boxes are matched and the quarter turn that puts the most
    plastic inside the model (``inside(points) -> bool``) wins. Raises if the sizes don't match."""
    a, b = bead_centres(tp)
    part = tp.part()
    pts = np.concatenate([a[part], b[part]])
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    lo[2] = 0.0  # the bed
    want = np.asarray(oriented_hi) - np.asarray(oriented_lo)
    probe = pts[:: max(len(pts) // 4000, 1)]
    best = None
    for turn in range(4):
        c, s = np.round(np.cos(turn * np.pi / 2)), np.round(np.sin(turn * np.pi / 2))
        q = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])
        size = np.abs(q @ (hi - lo))
        gap = float(np.abs(size - want).max())
        if gap > MAX_SIZE_GAP_MM + 0.05 * want.max():
            continue
        shift = (np.asarray(oriented_lo) + np.asarray(oriented_hi)) / 2 - q @ ((lo + hi) / 2)
        shift[2] = oriented_lo[2] - lo[2]
        score = float(np.mean(inside(probe @ q.T + shift)))
        if best is None or score > best[0]:
            best = (score, q, shift)
    if best is None:
        got = np.round(hi - lo, 1).tolist()
        raise ValueError(f"The G-code's part is {got} mm, the model {np.round(want, 1).tolist()} mm: "
                         "is it the same part, in the same orientation and scale?")
    if best[0] < 0.6:
        raise ValueError(f"Only {best[0]:.0%} of the printed lines fall inside the model: is it the same part and orientation?")
    return best[1], best[2]


def deposit(tp: Toolpath, grid: VoxelGrid, elems: np.ndarray, to_grid) -> dict:
    """What the printer puts in each analysis cube (``elems`` voxel indices): plastic by kind
    (mm³), the line-direction tensor sum(volume · d dᵀ) in the grid frame, and when the cube was
    first and last printed (s). ``to_grid(points or directions, is_direction)`` maps printer
    coordinates to the grid's frame."""
    sm = samples(tp, grid.h / 2)
    pts = to_grid(sm["points"], False)
    dirs = to_grid(sm["dirs"], True)
    idx = grid.index_of(pts)
    shape = np.array(grid.solid.shape)
    ok = np.all((idx >= 0) & (idx < shape), axis=1)
    elem_id = np.full(grid.solid.shape, -1)
    elem_id[tuple(elems.T)] = np.arange(len(elems))
    e = np.full(len(pts), -1)
    e[ok] = elem_id[tuple(idx[ok].T)]
    hit = e >= 0
    e, vol, dirs, kind, time = e[hit], sm["volume"][hit], dirs[hit], sm["kind"][hit], sm["time"][hit]
    n = len(elems)
    by_kind = np.stack([np.bincount(e, vol * (kind == k), minlength=n) for k in PART_KINDS], axis=1)
    tensor = np.stack([np.bincount(e, vol * dirs[:, i] * dirs[:, j], minlength=n) for i, j in ((0, 0), (1, 1), (2, 2), (1, 2), (0, 2), (0, 1))], axis=1)
    first = np.full(n, np.inf)
    last = np.full(n, -np.inf)
    np.minimum.at(first, e, time)
    np.maximum.at(last, e, time)
    return {"volume": by_kind, "tensor": tensor, "first_s": first, "last_s": last,
            "missed": float(sm["volume"][~hit].sum() / max(sm["volume"].sum(), 1e-12))}
