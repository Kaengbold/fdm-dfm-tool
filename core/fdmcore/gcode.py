"""Read a sliced part's G-code: every extruded line with its kind, direction, plastic, time and speed.

The detailed level of the simulation works from what the printer will really do instead of
estimates: which lines are walls, solid layers or sparse infill, which way each line runs (a
printed line is strong along itself and weak across), how much plastic sits where, and when
each spot was printed (for heat). Works with OrcaSlicer / Bambu Studio (``; FEATURE:``),
PrusaSlicer (``;TYPE:``) and Cura (``;TYPE:``) comments. Pure numpy, so it runs in the browser
on a G-code the user sliced themselves, and locally on one OrcaSlicer wrote for us.

For the heat model (T-473) it also keeps what changes the heat: each line's speed and why the
slicer printed it slower than set (only a slowdown for layer cooling may be undone), the fans,
travel and retractions, pressure advance, objects and tools, the firmware and the slicer's
settings. Big files (hundreds of MB) are read a few MB at a time.
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
ARC_MAX_POINTS = 720  # ... but never more: one bad line with a huge I/J would ask for millions
MAX_SIZE_GAP_MM = 2.0  # G-code and model sizes may differ this much (elephant-foot and hole compensation)


@dataclass
class Toolpath:
    """Extruded lines in printer coordinates (z = top of the line, as the nozzle sees it), and what
    else the printer does between them (travel, retractions, fans, pressure advance)."""

    p0: np.ndarray  # (N, 3) float32 start
    p1: np.ndarray  # (N, 3) float32 end
    volume: np.ndarray  # (N,) mm³ of plastic
    kind: np.ndarray  # (N,) uint8, WALL / SOLID / SPARSE / SUPPORT / OTHER
    layer: np.ndarray  # (N,) layer index
    layer_height: np.ndarray  # (N,) mm
    t0: np.ndarray  # (N,) s since the start of the print, when the line starts
    t1: np.ndarray  # (N,) s, when it ends
    layer_z: np.ndarray  # (L,) nozzle height of each layer
    total_s: float  # whole print, slicer's estimate when the file has one
    slicer: str
    speed: np.ndarray  # (N,) float32 mm/s, the feed rate the line is printed at
    set_speed: np.ndarray  # (N,) float32 mm/s, its feature's speed setting (NaN: not known)
    cause: np.ndarray  # (N,) uint8 why it is slower than set_speed (CAUSE_NAMES; 0: it isn't)
    feature: np.ndarray  # (N,) uint8 index into ``features``
    features: tuple  # the slicer's feature names ("Outer wall", "External perimeter", …)
    tool: np.ndarray  # (N,) uint8 extruder
    obj: np.ndarray  # (N,) int16 index into ``objects``, -1 outside any object
    objects: tuple  # object labels (EXCLUDE_OBJECT name, M486 / slicer label id)
    part_fan: np.ndarray  # (N,) float32 part cooling fan 0..1 while the line prints
    fans: dict  # every fan command: {"line", "t", "fan" (index into fan_names), "value" 0..1}
    fan_names: tuple  # "part", "P2", "P3", Klipper fan names
    pressure_advance: dict  # {"line", "t", "value"}: M900 K / SET_PRESSURE_ADVANCE / M572
    retracts: dict  # {"line", "t", "mm", "speed" mm/s, "moving"}: filament pulled back (< 0) or pushed again
    # (> 0); "moving": during a travel move (a wipe), so the nozzle doesn't stop for it
    dwells: dict  # {"line", "t", "s"}: G4 pauses
    motion: dict  # {"line", "t", "key" (index into MOTION_KEYS), "value"}: acceleration, jerk and speed limits
    travel: dict  # moves without plastic: {"p0", "p1", "t0", "t1", "speed", "line"}
    firmware: str  # marlin / klipper / rrf / bambu
    settings: dict  # the slicer's settings block, key -> text
    src: np.ndarray | None = None  # (N,) int32 text line each extruded line comes from (0-based, _line_blocks' split)

    def part(self) -> np.ndarray:
        return np.isin(self.kind, PART_KINDS)


def flow_cap(tp: "Toolpath") -> np.ndarray:
    """Per line, the most plastic per second its filament / the printer may melt (mm³/s; inf: not known):
    ``filament_max_volumetric_speed`` per extruder and PrusaSlicer's ``max_volumetric_speed``."""
    s, tools = tp.settings, int(tp.tool.max(initial=0)) + 1
    caps = []
    for i in range(tools):
        known = [v for v in (_number(s.get("filament_max_volumetric_speed"), 0, i), _number(s.get("max_volumetric_speed"), 0)) if v > 0]
        caps.append(min(known) if known else np.inf)
    return np.asarray(caps, float)[np.minimum(tp.tool, tools - 1)]


# Why a line is slower than its feature's speed setting. Only LAYER_TIME (the slicer slowing a whole
# layer so it has time to cool) may be undone by a better cooling plan; the others protect the print.
NOT_SLOWED, OVERHANG, BRIDGE, SMALL_PERIMETER, VOLUMETRIC_CAP, LAYER_TIME, UNKNOWN = range(7)
CAUSE_NAMES = ("", "overhang", "bridge", "small_perimeter", "volumetric_cap", "layer_time", "unknown")
SPEED_TOL = 0.02  # a speed "matches" a setting within 2 % (slicers round F to 0.1 mm/min … 1 mm/s)
CHUNK_BYTES = 4 << 20  # big files are read this much at a time

# Motion limits the G-code sets (mm/s, mm/s², mm), for the print timing (kinematics.py).
MOTION_KEYS = ("accel", "travel_accel", "jerk_x", "jerk_y", "junction_deviation", "square_corner_velocity",
               "accel_to_decel", "minimum_cruise_ratio", "max_accel_x", "max_accel_y", "max_accel_z",
               "max_speed_x", "max_speed_y", "max_speed_z")
_MOTION_WORDS = {  # command -> {word: key or keys}
    "M204": {"S": ("accel", "travel_accel"), "P": "accel", "T": "travel_accel"},  # Marlin, Bambu
    "M205": {"X": "jerk_x", "Y": "jerk_y", "J": "junction_deviation"},
    "M566": {"X": "jerk_x", "Y": "jerk_y"},  # RepRapFirmware, mm/min
    "M201": {"X": "max_accel_x", "Y": "max_accel_y", "Z": "max_accel_z"},
    "M203": {"X": "max_speed_x", "Y": "max_speed_y", "Z": "max_speed_z"},
}
_KLIPPER_LIMITS = {"ACCEL": ("accel", "travel_accel"), "SQUARE_CORNER_VELOCITY": "square_corner_velocity",
                   "ACCEL_TO_DECEL": "accel_to_decel", "MINIMUM_CRUISE_RATIO": "minimum_cruise_ratio"}


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
_SETTING = re.compile(r"^;\s*([a-z_][a-z0-9_]*)\s*=\s?(.*?)\s*$")
_LABEL_ID = re.compile(r"unique label id:\s*(\S+)")


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


def _params(line: str) -> dict[str, str]:
    """Klipper-style ``NAME=value`` words."""
    return dict(w.split("=", 1) for w in line.split(";", 1)[0].split()[1:] if "=" in w)


def _line_blocks(source):
    """The file's lines, a few MB at a time, so a 500 MB G-code never sits in memory as one list of
    lines (a Python string per line costs ~50 bytes on top of its text). ``source``: text, bytes, a
    binary or text file, or any iterable of text / bytes pieces."""
    if isinstance(source, (str, bytes, bytearray, memoryview)):
        pieces = (source[i:i + CHUNK_BYTES] for i in range(0, len(source), CHUNK_BYTES))
    elif hasattr(source, "read"):
        pieces = iter(lambda: source.read(CHUNK_BYTES), source.read(0))
    else:
        pieces = source
    rest_b, rest_s = b"", ""
    for piece in pieces:
        if isinstance(piece, str):
            cut = piece.rfind("\n") + 1
            text, rest_s = rest_s + piece[:cut], piece[cut:]
        else:
            piece = bytes(piece)
            cut = piece.rfind(b"\n") + 1  # never inside a UTF-8 character
            text, rest_b = (rest_b + piece[:cut]).decode("utf-8", "replace"), piece[cut:]
        if text:
            yield text.splitlines()
    tail = rest_s + rest_b.decode("utf-8", "replace")
    if tail:
        yield tail.splitlines()


class _Columns:
    """Rows gathered as tuples for one chunk of the file, then stored as compact numpy columns: a big
    print has millions of lines, and doubles for all of them would cost several times more."""

    def __init__(self, dtypes: tuple):
        self.dtypes, self.parts, self.rows, self.done = dtypes, [[] for _ in dtypes], [], 0

    def __len__(self) -> int:
        return self.done + len(self.rows)

    def flush(self) -> None:
        if self.rows:
            a = np.array(self.rows, dtype=float)
            for k, dt in enumerate(self.dtypes):
                self.parts[k].append(a[:, k].astype(dt))
            self.done += len(self.rows)
            self.rows = []

    def columns(self) -> list[np.ndarray]:
        """Each column joined; the pieces are freed column by column to keep the peak low."""
        self.flush()
        out = []
        for k, dt in enumerate(self.dtypes):
            parts, self.parts[k] = self.parts[k], []
            out.append(np.concatenate(parts) if parts else np.zeros(0, dt))
        return out


# x0 y0 z0 x1 y1 z1, filament mm, t0, t1, feed mm/min, state (index into the (kind, feature, object, tool, fan) table)
_LINE_COLS = ("f4",) * 6 + ("f8", "f8", "f8", "f4", "i4", "i4")  # x0 … z1, E, t0, t1, feed, state, text line
_TRAVEL_COLS = ("f4",) * 6 + ("f8", "f8", "f4", "i4")  # x0 … z1, t0, t1, feed, next line
_MOVES = {"G0": False, "G1": False, "G2": True, "G3": True}  # value: an arc


def parse(source, settings: dict | None = None) -> Toolpath:
    """Every extruding move as a straight line (arcs split into chords), with its speed, fan, tool and
    object, and the moves, retractions, fan and pressure-advance changes in between. Times come from
    the feed rates and are scaled to the slicer's own estimate when the file has one (it knows the
    printer's acceleration; we don't). ``source`` as for ``_line_blocks``; ``settings`` fills gaps in
    the file's own settings block (a ``.gcode.3mf``'s project_settings.config)."""
    x = y = z = 0.0
    e_abs, e_rel, xyz_rel = 0.0, False, False
    feed = 1800.0  # mm/min
    t = 0.0
    lines, travel = _Columns(_LINE_COLS), _Columns(_TRAVEL_COLS)
    fan_ev, pa_ev, retract_ev, dwell_ev, motion_ev = [], [], [], [], []
    features, feature_id, feature_kind = ["(none)"], {"(none)": 0}, [OTHER]
    objects, object_id, label_ids, named = [], {}, False, set()  # named: objects from PrusaSlicer-style labels
    fan_names, fan_id = ["part"], {"part": 0}
    found: dict[str, str] = {}  # the file's settings
    times: dict[int, float] = {}
    flags = set()  # firmware hints seen in commands
    generator, style = "", ""  # the "generated by" line; "FEATURE" (Orca, Bambu) or "TYPE" (Prusa, Cura) comments
    # What changes rarely is kept once per combination, not per line.
    feat, obj, tool, part_fan = 0, -1, 0, 0.0
    states, state_id = [], {}

    def state_of() -> int:
        key = (feat, obj, tool, part_fan)
        if key not in state_id:
            state_id[key] = len(states)
            states.append(key)
        return state_id[key]

    def index(name: str, names: list, ids: dict) -> int:
        if name not in ids:
            ids[name] = len(names)
            names.append(name)
        return ids[name]

    state = state_of()
    src = -1  # text line (0-based, as _line_blocks splits them), so gcode_rewrite can find each move again
    # Plain floats in the loop: a big print has millions of moves (and Pyodide is ~3× slower).
    for block in _line_blocks(source):
        lines.flush()
        travel.flush()
        add_line, add_travel = lines.rows.append, travel.rows.append
        for line in block:
            src += 1
            if not line:
                continue
            c = line[0]
            if c == ";":
                if (fm := _FEATURE.match(line)) is not None:
                    name = fm.group(1)
                    if name not in feature_id:
                        feature_kind.append(kind_of(name))
                    feat = index(name, features, feature_id)
                    state = state_of()
                    style = style or ("FEATURE" if "FEATURE" in line[:12] else "TYPE")
                elif " = " in line and (sm := _SETTING.match(line)) is not None:
                    found.setdefault(sm.group(1), sm.group(2))
                elif "time" in line:
                    for k, rx in enumerate(_TIME):
                        if k not in times and (tm := rx.search(line)) is not None:
                            times[k] = _seconds(tm.group(1))
                elif "printing object" in line:
                    if (lm := _LABEL_ID.search(line)) is not None:
                        label_ids = True
                        obj = -1 if "stop printing" in line else index(lm.group(1), objects, object_id)
                    elif not label_ids:  # PrusaSlicer: "; printing object NAME id:0 copy 0"
                        name = line.split("printing object", 1)[1].strip()
                        obj = -1 if "stop printing" in line else index(name, objects, object_id)
                        named.add(obj)
                    state = state_of()
                elif not generator and ("generated" in line.lower() or "BambuStudio" in line):
                    generator = line
                    if "BambuStudio" in line:
                        flags.add("bambu")
                continue
            if c == "G" and line[2:3] == " " and line[:2] in _MOVES:  # the hot path: most lines are moves
                body = line[3:]
                if ";" in body:
                    body = body[:body.index(";")]
                vx = vy = vz = ve = vi = vj = None
                try:
                    for w in body.split():
                        k = w[0]
                        if k == "X":
                            vx = float(w[1:])
                        elif k == "Y":
                            vy = float(w[1:])
                        elif k == "E":
                            ve = float(w[1:])
                        elif k == "F":
                            feed = max(float(w[1:]), 1.0)
                        elif k == "Z":
                            vz = float(w[1:])
                        elif k == "I":
                            vi = float(w[1:])
                        elif k == "J":
                            vj = float(w[1:])
                except ValueError:  # a broken word: the slow, forgiving way
                    w = _words(line)
                    vx, vy, vz, ve, vi, vj = (w.get(k) for k in "XYZEIJ")
                    feed = max(w.get("F", feed), 1.0)
                if xyz_rel:
                    nx, ny, nz = x + (vx or 0.0), y + (vy or 0.0), z + (vz or 0.0)
                else:
                    nx = x if vx is None else vx
                    ny = y if vy is None else vy
                    nz = z if vz is None else vz
                de = 0.0
                if ve is not None:
                    de = ve if e_rel else ve - e_abs
                    e_abs = e_abs + ve if e_rel else ve
                if _MOVES[line[:2]] and (vi is not None or vj is not None):
                    pts = _arc(np.array([x, y, z]), np.array([nx, ny, nz]), vi or 0.0, vj or 0.0, line[1] == "2")
                    lengths = np.linalg.norm(np.diff(pts, axis=0), axis=1)
                    total = float(lengths.sum())
                    dt = (total if total > 0 else abs(de)) / feed * 60
                    if de > 0 and total > 1e-6:
                        ts = t + np.concatenate([[0], np.cumsum(lengths)]) / total * dt
                        for k in range(len(lengths)):
                            add_line((*pts[k], *pts[k + 1], de * lengths[k] / total, ts[k], ts[k + 1], feed, state, src))
                    elif total > 1e-6:
                        add_travel((x, y, z, nx, ny, nz, t, t + dt, feed, len(lines)))
                else:
                    total = ((nx - x) ** 2 + (ny - y) ** 2 + (nz - z) ** 2) ** 0.5
                    dt = (total if total > 0 else abs(de)) / feed * 60
                    if de > 0 and total > 1e-6:
                        add_line((x, y, z, nx, ny, nz, de, t, t + dt, feed, state, src))
                    elif total > 1e-6:
                        add_travel((x, y, z, nx, ny, nz, t, t + dt, feed, len(lines)))
                if de != 0 and not (de > 0 and total > 1e-6):  # pulled back / pushed again (also while wiping)
                    retract_ev.append((len(lines), t, de, feed / 60, total > 1e-6))
                t += dt
                x, y, z = nx, ny, nz
                continue
            if c in "GM":
                cmd = line.split(None, 1)[0].split(";", 1)[0].upper()
            elif c in "SET":
                cmd = line.split(None, 1)[0].upper()
            else:
                continue
            if cmd in ("M106", "M107"):
                w = _words(line)
                p = int(w.get("P", 0))
                # Marlin fan 0 is the part fan; Bambu printers call it P1 (P2 aux, P3 chamber).
                name = "part" if p == 0 or (p == 1 and "bambu" in flags) else f"P{p}"
                value = 0.0 if cmd == "M107" else min(max(w.get("S", 255.0) / 255, 0.0), 1.0)
                if name == "part":
                    part_fan = value
                    state = state_of()
                fan_ev.append((len(lines), t, index(name, fan_names, fan_id), value))
            elif cmd == "SET_FAN_SPEED":
                pr = _params(line)
                flags.add("klipper")
                try:
                    fan_ev.append((len(lines), t, index(pr.get("FAN", "?"), fan_names, fan_id),
                                   min(max(float(pr.get("SPEED", 0)), 0.0), 1.0)))
                except ValueError:
                    pass
            elif cmd in ("M900", "M572", "SET_PRESSURE_ADVANCE"):
                if cmd == "SET_PRESSURE_ADVANCE":
                    flags.add("klipper")
                    value = _params(line).get("ADVANCE")
                else:
                    if cmd == "M572":
                        flags.add("rrf")
                    value = _words(line).get("K" if cmd == "M900" else "S")
                try:
                    pa_ev.append((len(lines), t, float(value)))
                except (TypeError, ValueError):
                    pass
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
                seconds = w.get("S", 0.0) + w.get("P", 0.0) / 1000
                dwell_ev.append((len(lines), t, seconds))
                t += seconds
            elif cmd in ("G10", "G11") and not ({"P", "L"} & set(_words(line))):  # firmware retraction
                retract_ev.append((len(lines), t, -np.inf if cmd == "G10" else np.inf, np.nan, 0))  # length, speed: settings
            elif cmd in _MOTION_WORDS or cmd == "SET_VELOCITY_LIMIT":
                if cmd == "SET_VELOCITY_LIMIT":
                    flags.add("klipper")
                    found_words = _params(line)
                elif cmd == "M203" and "rrf" in flags:  # RepRapFirmware gives speeds in mm/min
                    continue
                else:
                    found_words = _words(line)
                for word, key in _MOTION_WORDS.get(cmd, _KLIPPER_LIMITS).items():
                    try:
                        value = float(found_words[word]) / (60 if cmd == "M566" else 1)
                    except (KeyError, ValueError):
                        continue
                    for k in key if isinstance(key, tuple) else (key,):
                        motion_ev.append((len(lines), t, MOTION_KEYS.index(k), value))
            elif cmd == "M486":
                w = _words(line)
                if "S" in w:
                    obj = -1 if w["S"] < 0 else index(str(int(w["S"])), objects, object_id)
                    state = state_of()
            elif cmd.startswith("EXCLUDE_OBJECT_"):
                flags.add("klipper")
                if cmd in ("EXCLUDE_OBJECT_START", "EXCLUDE_OBJECT_END"):
                    obj = index(_params(line).get("NAME", "?"), objects, object_id) if cmd.endswith("START") else -1
                    state = state_of()
            elif c == "T" and cmd[1:].isdigit():
                if int(cmd[1:]) < 255:  # Bambu's T255 / T1000 are no extruders
                    tool = int(cmd[1:])
                    state = state_of()
            elif cmd in ("M620", "M621", "M1002"):
                flags.add("bambu")

    p0x, p0y, p0z, p1x, p1y, p1z, fil, t0, t1, feeds, state_of_line, src_of = lines.columns()
    if not len(fil):
        raise ValueError("No printed lines found: is this a G-code file from a slicer?")
    table = np.array(states, dtype=float).reshape(-1, 4)  # feature, object, tool, part fan
    feat_of = table[:, 0].astype(np.uint8)[state_of_line]
    obj_of = table[:, 1].astype(np.int16)[state_of_line]
    tool_of = table[:, 2].astype(np.uint8)[state_of_line]
    fan_of = table[:, 3].astype(np.float32)[state_of_line]
    del state_of_line
    kinds = np.array(feature_kind, np.uint8)[feat_of]
    named.discard(-1)
    if label_ids and named:  # OrcaSlicer writes both label styles: keep its label ids
        keep = [i for i in range(len(objects)) if i not in named]
        new_id = np.full(len(objects) + 1, -1, np.int16)  # [-1] stays -1
        new_id[keep] = np.arange(len(keep))
        obj_of = new_id[obj_of]
        objects = [objects[i] for i in keep]
    found = {**{k: _text(v) for k, v in (settings or {}).items() if k not in found}, **found}
    diameter = _number(found.get("filament_diameter"), 1.75)
    p0 = np.column_stack([p0x, p0y, p0z])
    del p0x, p0y, p0z
    p1 = np.column_stack([p1x, p1y, p1z])
    del p1x, p1y, p1z
    volume = fil * (np.pi * diameter**2 / 4)
    del fil
    total_s = times[min(times)] if times else t
    scale = total_s / t if t > 0 and total_s > 0 else 1.0
    t0 *= scale
    t1 *= scale
    tr = travel.columns()
    retract = np.array(retract_ev, dtype=float).reshape(-1, 5)
    firmware_retract = np.isinf(retract[:, 2])  # G10 / G11: the printer's own retraction length and speed
    retract[firmware_retract, 3] = np.where(retract[firmware_retract, 2] < 0, _number(found.get("retraction_speed"), np.nan),
                                            _number(found.get("deretraction_speed"), np.nan) or _number(found.get("retraction_speed"), np.nan))
    retract[firmware_retract, 2] = np.copysign(_number(found.get("retraction_length"), np.nan), retract[firmware_retract, 2])
    dwell = np.array(dwell_ev, dtype=float).reshape(-1, 3)
    motion = np.array(motion_ev, dtype=float).reshape(-1, 4)
    fan = np.array(fan_ev, dtype=float).reshape(-1, 4)
    pa = np.array(pa_ev, dtype=float).reshape(-1, 3)

    layer_z, layer, lh = _layers(kinds, p1[:, 2])
    speed = feeds / 60
    slicer = ("bambu" if "BambuStudio" in generator else "orca" if style == "FEATURE" else
              "prusa" if style == "TYPE" and "PrusaSlicer" in generator else "cura" if style == "TYPE" else "unknown")
    set_speed, cause = _slowdowns(features, feat_of, layer, np.linalg.norm(p1 - p0, axis=1), volume, speed, tool_of, found)
    return Toolpath(p0=p0, p1=p1, volume=volume, kind=kinds, layer=layer, layer_height=lh, t0=t0, t1=t1,
                    layer_z=layer_z, total_s=float(total_s), slicer=slicer, speed=speed, set_speed=set_speed, cause=cause,
                    feature=feat_of, features=tuple(features), tool=tool_of, obj=obj_of, objects=tuple(objects),
                    part_fan=fan_of, fans={"line": fan[:, 0].astype(np.int32), "t": fan[:, 1] * scale,
                                           "fan": fan[:, 2].astype(np.uint8), "value": fan[:, 3].astype(np.float32)},
                    fan_names=tuple(fan_names),
                    pressure_advance={"line": pa[:, 0].astype(np.int32), "t": pa[:, 1] * scale, "value": pa[:, 2]},
                    retracts={"line": retract[:, 0].astype(np.int32), "t": retract[:, 1] * scale, "mm": retract[:, 2],
                              "speed": retract[:, 3], "moving": retract[:, 4] > 0},
                    dwells={"line": dwell[:, 0].astype(np.int32), "t": dwell[:, 1] * scale, "s": dwell[:, 2]},
                    motion={"line": motion[:, 0].astype(np.int32), "t": motion[:, 1] * scale,
                            "key": motion[:, 2].astype(np.uint8), "value": motion[:, 3]},
                    travel={"p0": np.column_stack(tr[0:3]), "p1": np.column_stack(tr[3:6]), "t0": tr[6] * scale,
                            "t1": tr[7] * scale, "speed": tr[8] / 60, "line": tr[9]},
                    firmware=_firmware(found, flags), settings=found, src=src_of)


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
    n = int(np.clip(np.ceil(abs(sweep) * r / ARC_STEP_MM), 1, ARC_MAX_POINTS))
    a = a0 + sweep * np.linspace(0, 1, n + 1)
    pts = np.column_stack([c[0] + r * np.cos(a), c[1] + r * np.sin(a), np.linspace(p0[2], p1[2], n + 1)])
    pts[-1] = p1
    return pts


def _text(v) -> str:
    """A setting from project_settings.config (a list per filament there) as the G-code block writes it."""
    return ",".join(map(str, v)) if isinstance(v, (list, tuple)) else str(v)


def _number(text: str | None, default: float, index: int = 0) -> float:
    """One number from a setting ("16", "16,21" per filament → the ``index``-th, "50%" → 50)."""
    try:
        parts = str(text).split(",")
        return float(parts[min(index, len(parts) - 1)].strip().rstrip("%"))
    except (TypeError, ValueError):
        return default


def _layers(kinds: np.ndarray, z1: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Layer heights, each line's layer and layer height. Layers are the heights the part's own lines
    are printed at (support may use its own layer heights); every other line belongs to the part
    layer at or above it."""
    z = np.round(z1.astype(float), 3)
    part = np.isin(kinds, PART_KINDS)
    layer_z = np.unique(z[part]) if part.any() else np.unique(z)
    layer = np.minimum(np.searchsorted(layer_z, z - 1e-3), len(layer_z) - 1).astype(np.int32)
    below = np.concatenate([[0.0], layer_z[:-1]])
    lh = (layer_z - below)[layer]
    if not part.all():  # support / skirt lines: their own height step, at most the part's
        heights = np.unique(z)
        own = np.diff(np.concatenate([[0.0], heights]))[np.searchsorted(heights, z)]
        lh = np.where(part, lh, np.minimum(own, np.median(lh[part]) if part.any() else own))
    return layer_z, layer, lh.astype(np.float32)


def _firmware(settings: dict, flags: set) -> str:
    """Which firmware the file is for: Bambu printers by name or their own commands, else the
    slicer's G-code flavour, else the commands seen."""
    if "bambu" in flags or settings.get("printer_model", "").startswith("Bambu Lab"):
        return "bambu"
    flavor = settings.get("gcode_flavor", "").lower()
    if "klipper" in flavor:
        return "klipper"
    if "reprapfirmware" in flavor or flavor == "rrf":
        return "rrf"
    if flavor:
        return "marlin"
    return "klipper" if "klipper" in flags else "rrf" if "rrf" in flags else "unknown"


def _role(feature: str) -> str | None:
    """What a feature is, in the terms the speed settings use."""
    f = feature.lower()
    if "overhang" in f:
        return "overhang"
    if "bridge" in f:
        return "internal_bridge" if "internal" in f else "bridge"
    if "support" in f:
        return "support_interface" if "interface" in f else "support"
    if f in ("outer wall", "external perimeter", "wall-outer"):
        return "outer"
    if "wall" in f or "perimeter" in f:
        return "inner"
    for role in ("gap", "ironing", "top", "bottom"):
        if role in f:
            return role
    if "solid" in f or f == "skin":
        return "solid"
    if "infill" in f or f == "fill":
        return "sparse"
    if "skirt" in f or "brim" in f:
        return "skirt"
    return None


# role -> speed setting, OrcaSlicer / Bambu Studio and PrusaSlicer. Bridges and overhanging walls are set
# against what they are part of, so their own slower speed shows as the cause.
_ORCA_SPEEDS = {
    "outer": "outer_wall_speed", "inner": "inner_wall_speed", "overhang": "outer_wall_speed",
    "sparse": "sparse_infill_speed", "solid": "internal_solid_infill_speed", "bottom": "internal_solid_infill_speed",
    "top": "top_surface_speed", "bridge": "internal_solid_infill_speed", "internal_bridge": "internal_solid_infill_speed",
    "gap": "gap_infill_speed", "support": "support_speed", "support_interface": "support_interface_speed",
    "ironing": "ironing_speed", "skirt": "skirt_speed",
}
_PRUSA_SPEEDS = {
    "outer": "external_perimeter_speed", "inner": "perimeter_speed", "overhang": "perimeter_speed",
    "sparse": "infill_speed", "solid": "solid_infill_speed", "bottom": "solid_infill_speed",
    "top": "top_solid_infill_speed", "bridge": "solid_infill_speed", "internal_bridge": "solid_infill_speed",
    "gap": "gap_fill_speed", "support": "support_material_speed",
    "support_interface": "support_material_interface_speed", "ironing": "ironing_speed", "skirt": "perimeter_speed",
}
# setting -> the setting a "50%" in it refers to
_ORCA_BASE = {"small_perimeter_speed": "outer_wall_speed", "internal_bridge_speed": "bridge_speed",
              **{f"overhang_{k}_4_speed": "outer_wall_speed" for k in range(1, 5)}}
_PRUSA_BASE = {"solid_infill_speed": "infill_speed", "top_solid_infill_speed": "solid_infill_speed",
               "external_perimeter_speed": "perimeter_speed", "support_material_interface_speed": "support_material_speed",
               "small_perimeter_speed": "perimeter_speed", **{f"overhang_speed_{k}": "perimeter_speed" for k in range(4)}}
_INFILL_ROLES = ("sparse", "solid", "bottom", "top", "bridge", "internal_bridge")


def _speed(settings: dict, key: str, base: dict, auto: float = np.nan) -> float:
    """A speed setting in mm/s, a percentage resolved against its base setting; 0 or missing: ``auto``."""
    text = str(settings.get(key, "")).split(",")[0].strip()
    value = _number(text, np.nan)
    if text.endswith("%"):
        return value / 100 * _speed(settings, base[key], base, auto) if key in base else np.nan
    return value if value > 0 else auto


def _slowdowns(features: list[str], feature: np.ndarray, layer: np.ndarray, length: np.ndarray, volume: np.ndarray,
               speed: np.ndarray, tool: np.ndarray, settings: dict) -> tuple[np.ndarray, np.ndarray]:
    """Each line's set speed (its feature's speed setting; the first-layer speed on the first layer)
    and why it is printed slower, from the slicer's settings block. Slicers slow lines for overhangs,
    bridges and small loops at their own set speeds, cap every line at the filament's flow limit, and
    slow a layer that would print faster than its minimum layer time by capping all its lines at one
    speed (OrcaSlicer and Bambu Studio; PrusaSlicer slows the fastest lines first, which is the same):
    so a layer whose fastest lines all sit at one speed below what they were set to was slowed for cooling."""
    n = len(speed)
    set_speed = np.full(n, np.nan, np.float32)
    cause = np.zeros(n, np.uint8)
    orca = "outer_wall_speed" in settings
    if not orca and "perimeter_speed" not in settings:
        return set_speed, cause  # no settings block (Cura, or cut off): nothing to compare with
    s = settings
    if orca:
        table, base = _ORCA_SPEEDS, _ORCA_BASE
        first = (_speed(s, "initial_layer_speed", base), _speed(s, "initial_layer_infill_speed", base))
        bridge = (_speed(s, "bridge_speed", base), _speed(s, "internal_bridge_speed", base, _speed(s, "bridge_speed", base)))
        overhang = [_speed(s, f"overhang_{k}_4_speed", base) for k in range(1, 5)] if s.get("enable_overhang_speed", "1") != "0" else []
        small = _speed(s, "small_perimeter_speed", base)
        auto, min_key, outer_kept = np.nan, "slow_down_min_speed", s.get("dont_slow_down_outer_wall", "0") == "1"
        max_flow = [_number(s.get("filament_max_volumetric_speed"), 0, i) for i in range(int(tool.max(initial=0)) + 1)]
    else:
        table, base = _PRUSA_SPEEDS, _PRUSA_BASE
        auto = _speed(s, "max_print_speed", {})  # 0 = "auto": as fast as the flow limit allows
        fl = str(s.get("first_layer_speed", "")).strip()
        first = (_number(fl, np.nan),) * 2 if not fl.endswith("%") else None
        bridge = (_speed(s, "bridge_speed", base),) * 2
        overhang = [_speed(s, f"overhang_speed_{k}", base) for k in range(4)] if s.get("enable_dynamic_overhang_speeds", "0") == "1" else []
        small = _speed(s, "small_perimeter_speed", base)
        min_key, outer_kept = "min_print_speed", False
        caps = [_number(s.get("max_volumetric_speed"), 0)]
        max_flow = [min([c for c in caps + [_number(s.get("filament_max_volumetric_speed"), 0, i)] if c > 0], default=0)
                    for i in range(int(tool.max(initial=0)) + 1)]

    roles = [_role(f) for f in features]
    normal = np.array([_speed(s, table[r], base, auto) if r in table else np.nan for r in roles], np.float32)
    if orca and "skirt" in roles:  # skirt_speed 0: printed at the first layer speed
        normal[[i for i, r in enumerate(roles) if r == "skirt" and not normal[i] > 0]] = first[0]
    if first is None:  # PrusaSlicer "50%": of each feature's own speed
        at_first = normal * _number(fl, 100) / 100
    else:
        at_first = np.array([first[1] if r in _INFILL_ROLES else first[0] for r in roles], np.float32)
        at_first = np.where(np.isfinite(normal), at_first, np.nan)  # custom G-code, prime tower: no setting
    set_speed = np.where(layer == 0, at_first[feature], normal[feature]).astype(np.float32)

    per_feature = lambda *names: np.array([r in names for r in roles])[feature]
    wall, is_bridge, is_over = per_feature("outer", "inner"), per_feature("bridge", "internal_bridge"), per_feature("overhang")
    bridge_speed = np.where(per_feature("internal_bridge"), bridge[1], bridge[0])
    flow = np.asarray(max_flow, float)[np.minimum(tool, len(max_flow) - 1)]
    area = volume / np.maximum(length, 1e-9)  # mm² of bead cross-section
    cap = np.where(flow > 0, flow / np.maximum(area, 1e-9), np.inf)
    near = lambda a, b: np.isfinite(b) & (np.abs(a - b) <= np.maximum(SPEED_TOL * b, 0.15))
    slow = np.isfinite(set_speed) & (speed < set_speed * (1 - SPEED_TOL) - 0.1)

    left = slow.copy()

    def mark(which: np.ndarray, why: int) -> None:
        """The first rule that fits a slowed line names its cause."""
        cause[left & which] = why
        left[which] = False

    over_speeds = [v for v in overhang if v > 0]
    mark(is_bridge & (near(speed, bridge_speed) | (speed >= bridge_speed)), BRIDGE)
    if over_speeds or is_over.any():
        on_overhang = np.zeros(n, bool)
        for v in over_speeds + [bridge[0]]:
            on_overhang |= near(speed, v)
        mark((wall | is_over) & on_overhang, OVERHANG)
    if small > 0:
        mark(wall & near(speed, small), SMALL_PERIMETER)
    mark(near(speed, cap), VOLUMETRIC_CAP)

    # Cooling: the fastest adjustable line of the layer is slower than it was set to, and others share its speed.
    adjustable = np.isfinite(set_speed) & ~is_bridge & ~is_over & ~(outer_kept & per_feature("outer"))
    n_layers = int(layer.max(initial=0)) + 1
    top = np.zeros(n_layers)
    np.maximum.at(top, layer[adjustable], speed[adjustable])
    v_layer = top[layer]
    held = adjustable & near(speed, v_layer) & (speed < np.minimum(set_speed, cap) * (1 - SPEED_TOL))
    cooled = np.zeros(n_layers, bool)
    cooled[layer[held]] = True
    min_speed = _number(s.get(min_key), 0)
    mark(cooled[layer] & (near(speed, v_layer) | (min_speed > 0) & near(speed, min_speed)), LAYER_TIME)

    mark(is_bridge, BRIDGE)
    mark(is_over | wall & bool(over_speeds) & (speed >= min(over_speeds, default=np.inf) * (1 - SPEED_TOL)), OVERHANG)
    cause[left] = UNKNOWN
    return set_speed, cause


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


def time_lost(tp: Toolpath) -> np.ndarray:
    """Seconds each slowdown cause adds to the print (index = cause), at the feed rates: the time the
    slowed lines take minus the time they would take at their set speed."""
    slow = tp.cause > 0
    length = np.linalg.norm(tp.p1[slow] - tp.p0[slow], axis=1)
    extra = length / tp.speed[slow] - length / tp.set_speed[slow]
    return np.bincount(tp.cause[slow], extra, minlength=len(CAUSE_NAMES))


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


SPOT_MM = 2.0  # line-by-line heat: the bed is split into cells this wide, per layer


def spot_heat(tp: Toolpath, to_frame, profile) -> dict:
    """Heat line by line within the layers: for each small spot of each layer, how long until the next
    layer covers it, and how warm it still is then. ``to_frame`` maps printer points to the frame the
    result is wanted in (x, y on the bed, z up).

    A spot's time is the median print time of the part's lines within 3 × 3 cells (6 mm) around it, so
    a single line that ends a layer next to where the next one starts doesn't count as a hot spot. Its
    temperature cools from the nozzle's toward the printer's air exponentially; the time constant is
    set so a spot covered after exactly the filament's minimum layer time (what the slicer slows a
    layer down to) is where the layer heat check starts to warn. Rough: no sideways heat flow, no bed."""
    from .heat import AMBIENT_C
    from .profile import load_data

    s = samples(tp, SPOT_MM / 2)
    pts = to_frame(s["points"])
    cell = np.floor(pts[:, :2] / SPOT_MM).astype(np.int64)
    layer = s["layer"].astype(np.int64)
    lo = cell.min(axis=0) - 1
    nx, ny = (cell.max(axis=0) - lo + 2).tolist()
    key = lambda l, c: (l * nx + (c[:, 0] - lo[0])) * ny + (c[:, 1] - lo[1])
    near = []  # every sample counts for its own cell and the 8 around it
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            near.append(key(layer, cell + [dx, dy]))
    keys = np.concatenate(near)
    times = np.tile(s["time"], 9)
    order = np.lexsort((times, keys))
    keys, times = keys[order], times[order]
    uniq, start, count = np.unique(keys, return_index=True, return_counts=True)
    median = times[start + count // 2]
    own = np.unique(key(layer, cell))  # cells that really have plastic
    has = np.isin(uniq, own)
    uniq, median = uniq[has], median[has]
    above = uniq + nx * ny  # the same spot one layer up
    j = np.searchsorted(uniq, above)
    covered = (j < len(uniq)) & (uniq[np.minimum(j, len(uniq) - 1)] == above)
    dt = np.where(covered, median[np.minimum(j, len(uniq) - 1)] - median, np.nan)

    mat = profile.material
    t_air, t_noz = AMBIENT_C[profile.enclosure], float(mat["nozzle_temp_c"])
    warn_at = float(mat["softening_c"]) + load_data("thresholds")["cooling"]["soft_margin_c"]
    tau = float(mat["min_layer_time_s"]) / np.log((t_noz - t_air) / max(warn_at - t_air, 1.0))
    temp = t_air + (t_noz - t_air) * np.exp(-np.maximum(dt, 0) / tau)
    l = uniq // (nx * ny)
    rest = uniq % (nx * ny)
    xy = (np.stack([rest // ny, rest % ny], axis=1) + lo + 0.5) * SPOT_MM
    return {"layer": l[covered], "xy": xy[covered], "z": tp.layer_z[l[covered]], "seconds": dt[covered], "temp_c": temp[covered],
            "cell_mm": SPOT_MM}


# Pauses added to a G-code the user sliced (T-322): Bambu Studio reads no pause from a 3MF it didn't
# write, so for Bambu printers the pause goes into the sliced file instead.
_LAYER_MARK = re.compile(r"^;\s*(CHANGE_LAYER|LAYER_CHANGE)\s*$")
_LAYER_Z = re.compile(r"^;\s*(?:Z_HEIGHT|Z):\s*([\d.]+)")
_PAUSE_SETTING = re.compile(r"^;\s*(?:machine_pause_gcode|pause_print_gcode)[ \t]*=[ \t]*(.*)$", re.M)  # an empty one: not the next line
_Z_MOVE = re.compile(r"^G[0-3]\b[^;]*\bZ", re.I)
_EXTRUDE = re.compile(r"^G[1-3]\b[^;]*\b[XY][^;]*\bE(?!-)[\d.]", re.I)
BAMBU_PAUSE = "M400 U1"  # what Bambu Lab's own printer profiles pause with (machine_pause_gcode)
PAUSE_MARK = "; PAUSE_PRINTING"  # the comment Bambu Studio / OrcaSlicer put before their own pause


def pause_command(text: str) -> str:
    """The printer's pause command as the slicer stored it in the file's settings (Bambu Studio and
    OrcaSlicer ``machine_pause_gcode``, PrusaSlicer ``pause_print_gcode``). If it isn't there:
    Bambu's for a Bambu Studio file, else M601 (Marlin, Prusa)."""
    m = _PAUSE_SETTING.search(text)
    cmd = m.group(1).strip().replace("\\n", "\n").strip() if m else ""
    if cmd:
        return cmd
    return BAMBU_PAUSE if "BambuStudio" in text[:4000] else "M601"


_SET_TEMP = re.compile(r"^(M10[49])\b([^;]*)", re.I)
_S_VALUE = re.compile(r"\bS[\d.]+", re.I)


def add_temperatures(text: str, bands: list[tuple[float, float]]) -> tuple[str, list[dict]]:
    """``text`` (a sliced temperature tower) with the nozzle set to each band's temperature from the first layer above
    its start (``bands``: (z_from mm, °C), lowest first; a layer's mark gives its top, so the layer ending at z_from is
    still the band below): an M104 right after that layer's mark, and the slicer's own nozzle temperature changes after
    the first band starts (its second-layer temperature) set to the band's too, so they don't undo it. Turning the
    heater off (S0, the end of the print) is left alone. Returns the new text and where each band starts: {"z", "temp_c"}."""
    todo = sorted((float(z), float(t)) for z, t in bands)
    out, done = [], []
    nl = "\r\n" if "\r\n" in text[:20000] else "\n"
    marked = seen_mark = False
    current = None
    for line in text.split(nl):
        s = line.strip()
        m = _SET_TEMP.match(s)
        s_val = _S_VALUE.search(m.group(2)) if m else None
        heater_off = s_val is not None and float(s_val.group(0)[1:]) == 0
        if current is not None and m and not heater_off and " T1" not in m.group(2).upper():  # the slicer's own change: this band's instead
            line = f"{m.group(1).upper()} {_S_VALUE.sub(f'S{current:g}', m.group(2).strip()) if _S_VALUE.search(m.group(2)) else f'S{current:g}'}".rstrip()
        out.append(line)
        if _LAYER_MARK.match(s):
            marked = seen_mark = True
        elif marked and (mz := _LAYER_Z.match(s)) is not None:
            marked = False
            z = float(mz.group(1))
            due = [b for b in todo[len(done):] if z > b[0] + 1e-4]
            if due:
                current = due[-1][1]
                done += [{"z": z, "temp_c": t} for _, t in due]
                out.append(f"M104 S{current:g} ; temperature tower: {current:g} °C from here")
    if not seen_mark:
        raise ValueError("This G-code has no layer marks (; CHANGE_LAYER or ;LAYER_CHANGE): slice the tower in "
                         "Bambu Studio, OrcaSlicer or PrusaSlicer")
    if len(done) < len(todo):
        raise ValueError(f"The print ends below {todo[len(done)][0]:.1f} mm, where a band starts: is this the temperature tower's G-code?")
    return nl.join(out), done


def add_pauses(text: str, tops: list[float]) -> tuple[str, list[dict]]:
    """``text`` with a pause before the first layer above each pocket top in ``tops`` (mm, the ``top_z``
    of pause_to_insert), placed like the slicers place their own: after the move up to that layer
    (filament pulled back, nozzle raised), before it prints anything. Also returns where each went:
    {"top", "z": the layer it pauses before, "command"}."""
    cmd = pause_command(text)
    block = [PAUSE_MARK, *cmd.split("\n")]
    todo = sorted({round(float(t), 4) for t in tops})
    out, done = [], []
    nl = "\r\n" if "\r\n" in text[:20000] else "\n"  # keep the file's line endings
    marked = seen_mark = waiting = False  # waiting: this layer pauses once it has moved up
    for line in text.split(nl):
        s = line.strip()
        is_mark = bool(_LAYER_MARK.match(s))
        if waiting and (is_mark or _EXTRUDE.match(s)):  # no move up before it prints: pause right here
            out += block
            waiting = False
        out.append(line)
        if waiting and _Z_MOVE.match(s):
            out += block
            waiting = False
        if is_mark:
            marked = seen_mark = True
        elif marked and (m := _LAYER_Z.match(s)) is not None:
            marked = False
            z = float(m.group(1))
            hit = [t for t in todo[len(done):] if z > t + 1e-4]
            done += [{"top": t, "z": z, "command": cmd} for t in hit]
            waiting = waiting or bool(hit)
    if waiting:
        out += block
    if not seen_mark:
        raise ValueError("This G-code has no layer marks (; CHANGE_LAYER or ;LAYER_CHANGE): slice the part in "
                         "Bambu Studio, OrcaSlicer or PrusaSlicer")
    if len(done) < len(todo):
        raise ValueError(f"The print ends below {todo[len(done)]:.2f} mm, where it should pause: is this "
                         "the G-code of this part, in this orientation?")
    return nl.join(out), done
