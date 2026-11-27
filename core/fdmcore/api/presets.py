from __future__ import annotations

import numpy as np

from ..mesh import Mesh, to_binary_stl
from ..profile import DATA as DATA_DIR, Profile, WARPING_TEST_MM, load_data, presets
from ..service import CREEP_LIFE_DAYS, CREEP_TIMES_MIN
from . import check as _check
from . import session as _S
from . import strength as _strength


def get_presets() -> dict:
    """All presets, plus how to use each calibration print (``calibration``) for the My materials form."""
    return {**presets(), "calibration": CALIBRATION_HOW, "creep_test": CREEP_TEST}


def custom_presets(printers: dict | None = None, materials: dict | None = None) -> dict:
    """Set the user's own printers / materials (see profile.set_custom_presets); returns all presets."""
    from ..profile import set_custom_presets

    set_custom_presets(printers, materials)
    if _S._session is not None:
        _S._session.solved = None
    return get_presets()


def expert_settings() -> dict:
    """Every number the checks use, for the Expert settings window: the analysis grids and all of
    thresholds.yaml, grouped by section, each with its default, the value in use and the note from the
    file (what it does and its source). Nothing hidden."""
    from ..profile import _expert, _read_data

    analysis = {"name": "analysis", "title": "Analysis grids", "about": "How finely the part is cut into cubes for the simulations.",
                "values": [{"path": f"analysis.{k}", "default": d, "value": _S._grid(k), "min": lo, "max": hi, "note": note}
                           for k, (d, lo, hi, note) in _S.ANALYSIS.items()]}
    notes, about = _yaml_notes(DATA_DIR / "thresholds.yaml")
    over = _expert.get("thresholds", {})
    sections = {}

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, [*path, str(k)])
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, [*path, str(i)])
        elif isinstance(node, (int, float)) and not isinstance(node, bool):
            key = ".".join(path)
            note = next((notes[".".join(path[:n])] for n in range(len(path), 0, -1) if ".".join(path[:n]) in notes), "")
            sections.setdefault(path[0], []).append({"path": f"thresholds.{key}", "default": node, "value": over.get(key, node), "note": note})

    walk(_read_data("thresholds"), [])
    out = [analysis] + [{"name": k, "title": k.replace("_", " ").capitalize(), "about": about.get(k, ""), "values": v} for k, v in sections.items()]
    return {"sections": out}


def _yaml_notes(path) -> tuple[dict[str, str], dict[str, str]]:
    """Comments of a YAML file: the one after each ``key: value`` (by dotted path) and the full-line
    comments inside each top-level section (its description)."""
    import re

    notes, about, stack = {}, {}, []
    for line in path.read_text(encoding="utf-8").splitlines():
        indent = len(line) - len(line.lstrip())
        text = line.strip()
        if text.startswith("#"):
            if stack:
                about[stack[0][1]] = (about.get(stack[0][1], "") + " " + text.lstrip("# ")).strip()
            continue
        m = re.match(r"([\w-]+):\s*([^#]*?)\s*(?:#\s*(.*))?$", text)
        if not m:
            continue
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, m.group(1)))
        if m.group(3):
            notes[".".join(k for _, k in stack)] = m.group(3)
    return notes, about


def set_expert(values: dict[str, float]) -> dict:
    """Apply the Expert settings window: ``{"thresholds.features.vanish_below_nozzle": 0.6,
    "analysis.strength_voxels": 20000}``; values not given go back to their defaults."""
    from ..profile import set_expert as set_data

    grids, data = {}, {}
    for path, value in (values or {}).items():
        group, _, rest = path.partition(".")
        if group == "analysis":
            if rest not in _S.ANALYSIS:
                raise KeyError(f"Unknown setting {path}")
            lo, hi = _S.ANALYSIS[rest][1:3]
            grids[rest] = min(max(float(value), lo), hi)
        else:
            data.setdefault(group, {})[rest] = float(value)
    set_data(data)
    _S._analysis.clear()
    _S._analysis.update(grids)
    if _S._session is not None:
        _S._session.solved = None  # results depend on these
        _S._session.warp = None
        if _S._session.gcode is not None:
            _S._session.gcode.pop("heat", None)
            _S._session.gcode.pop("heat_ranges", None)
    return expert_settings()


CALIBRATION = ("layer_strength", "warping", "fit", "overhang", "bridge", "sag", "snap", "temp", "ironing", "creep", "all")


CREEP_TEST = {"lever_mm": 75, "width_mm": 15, "thickness_mm": 4, "mass_g": 500, "hole_mm": 6,
              "times_min": CREEP_TIMES_MIN, "design_days": CREEP_LIFE_DAYS}


# Nominal bending stress at the clamp: M / section modulus, in N/mm².
CREEP_TEST["stress_mpa"] = round(6 * (CREEP_TEST["mass_g"] / 1000) * 9.80665 * CREEP_TEST["lever_mm"]
                                / (CREEP_TEST["width_mm"] * CREEP_TEST["thickness_mm"] ** 2), 2)


# What to print and measure per test print: shown in the web app's My materials form and by the MCP tool.
CALIBRATION_HOW = {
    "layer_strength": "A 98 mm dog-bone: thin in the middle, wide with a hole at each end. Print one lying flat and one "
                      "standing on its end (a brim helps it stay up). Hang each by one hole and pull on the other with a "
                      "luggage scale (or add weight) until it breaks in the thin middle. Standing ÷ flat is your layer strength.",
    "warping": "Bars 40, 80, 120 and 160 mm long. Print them without a brim (in your enclosure if you have one): "
               "the longest one that stays flat on the bed is how long a part can get before its corners lift.",
    "fit": "A 6 mm peg and holes 6.0 … 6.5 mm (the notched end is 6.0). Try the peg in each hole: "
           "note the one it pushes into snugly and the one it slides and turns in.",
    "overhang": "Seven blocks leaning 40° … 70° (the short end is 40°), printed without support: "
                "the steepest one that's still clean is how far this material can lean out.",
    "bridge": "Bridges 10 … 50 mm long between pillars, printed without support: "
              "the longest one that doesn't sag is how far it can bridge.",
    "sag": "Five Ø3 mm pins on a plate, 10 to 40 mm tall (the height is written by each). The higher it goes, the fewer "
           "pins are left, so each layer prints faster and stays hotter. Slice it, load the G-code here and download the "
           "test G-code (it switches the slicer's slowdown for cooling off), print it, then note the height where the "
           "tallest pin first droops or blobs (or that none does).",
    "snap": "A thin wall 80 mm long and 40 mm tall. Slice it, load the G-code here and download the test G-code: it "
            "turns the fan down step by step, every 8 mm from 100 % at the bottom to 20 % at the top. Print it, then "
            "snap pieces off by hand from the top down: note the height below which it breaks easily along a layer.",
    "temp": "A tower of six blocks, each printed at its own temperature (written on its front, hottest at the bottom): "
            "slice it, load the G-code here and download the test G-code (it sets each block's temperature), print it, "
            "then pick the block with the cleanest overhang and sharpest corners, without strings or a rough, dull surface. "
            "OrcaSlicer and Bambu Studio also make their own (Calibration, Temperature).",
    "ironing": "Five steps, each top ironed with a different flow (written on it, in %). Open the 3MF in OrcaSlicer or "
               "Bambu Studio and print it as it is (the flows are already set for each height), then pick the smoothest "
               "top: too little flow leaves lines, too much leaves ridges and blobs.",
    "creep": "Print the arm flat with 0.2 mm layers, 100% infill and supports off. Clamp the wide end to a sturdy table "
             "with the table edge at the end of the taper. Fix a ruler upright beside the pointed tip and mark the "
             "height of the tip's lower edge before loading. Hang a bottle and cord of 500 g total from the hole "
             "above a catch tray. "
             f"The load puts about {CREEP_TEST['stress_mpa']:g} MPa of bending stress on the arm at the clamp. "
             "Keep it at 18–30 °C away from sun or heaters. With the weight still on, measure the tip drop from the "
             "unloaded mark at 1 minute, 1 hour, 1 day, 1 week and 4 weeks.",
    "all": "The starter tests on one plate (about 170 × 175 mm): the warping bars, two dog-bones (one lies flat, one stands on "
           "its end: give it a brim), the fit gauge with its peg, the overhang blocks and the bridges. Each piece has its "
           "size written on it (the fit holes: 6 mm plus the number). Print it without support, then read each test as below.",
}


FIT_GAUGE_GAPS = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5)  # hole sizes of the fit gauge: 6 mm + these


# Layer strength dog-bone (T-387), the shape of tensile test bars (ASTM D638 / ISO 527): the middle is the weakest
# section, so it breaks there and not at a hole. The old straight bar had only 4 of its 10 mm left beside each
# hole and broke there. A pin in a hole puts 2.5-3x the average stress beside it, so the ends carry 9x the
# middle's section; the strength check (pin on a third of the hole) then finds the most stress in the middle,
# lying and standing. A thin middle also keeps the break weight within a luggage scale (PLA ~50 MPa x 12 mm²).
# Ends: width, length, hole (an M5 bolt, a string); middle: width, length; round between them; thickness (mm).
DOGBONE = {"end_w": 34.0, "end_l": 22.0, "hole_d": 6.0, "mid_w": 3.0, "mid_l": 30.0, "round": 12.0, "thick": 4.0}


OVERHANG_TEST_DEG = (40, 45, 50, 55, 60, 65, 70)  # from vertical


BRIDGE_TEST_MM = (10, 20, 30, 40, 50)


LABEL_MM = 0.6  # raised numbers: three 0.2 mm layers, readable and quick to print


# Seven-segment digits drawn from rectangles (no buffers or fonts: Pyodide's GEOS stays happy, T-268).
_SEGMENTS = {"0": "abcdef", "1": "bc", "2": "abdeg", "3": "abcdg", "4": "bcfg", "5": "acdfg", "6": "acdefg",
             "7": "abc", "8": "abcdefg", "9": "abcdfg"}


def _label(text: str, x: float, y: float, top: float, height: float, sideways: bool = False) -> np.ndarray:
    """Raised digits (and '.') ``height`` mm tall, lower left at (x, y), standing ``LABEL_MM`` on a face at ``top``
    (sunk a little into it, so the slicer joins them). ``sideways``: read along y (turned 90°)."""
    from shapely.geometry import box as sbox
    from shapely.ops import unary_union

    from ..additions import extrude

    h, t = height, height / 5
    w = 0.6 * h
    seg = {"a": (0, h - t, w, h), "b": (w - t, h / 2, w, h), "c": (w - t, 0, w, h / 2), "d": (0, 0, w, t),
           "e": (0, 0, t, h / 2), "f": (0, h / 2, t, h), "g": (0, h / 2 - t / 2, w, h / 2 + t / 2)}
    boxes, at = [], 0.0
    for ch in text:
        if ch == ".":
            boxes.append(sbox(at, 0, at + t, t))
            at += 2 * t
            continue
        boxes += [sbox(at + x0, y0, at + x1, y1) for x0, y0, x1, y1 in (seg[s] for s in _SEGMENTS[ch])]
        at += w + t
    shape = unary_union(boxes)
    tris = np.concatenate([extrude(p, top - 0.2, top + LABEL_MM) for p in getattr(shape, "geoms", [shape])])
    if sideways:  # (u, v) -> (-v, u): reads along +y; a rotation, so the winding stays outward
        tris = np.stack([height - tris[..., 1], tris[..., 0], tris[..., 2]], axis=-1)
    return tris + [x, y, 0]


def _lying(profile_xz, y0: float, depth: float) -> np.ndarray:
    """A profile drawn in x/z, extruded along y from y0 (extrude works in x/y, then turned up)."""
    from ..additions import extrude

    tris = extrude(profile_xz, y0, y0 + depth)[:, :, [0, 2, 1]]  # (x, z, y) -> (x, y, z)
    return tris[:, ::-1]  # swapping two axes mirrors: flip the winding back


def _gap_text(gap: float) -> str:
    return "0" if gap == 0 else f"{gap:.1f}"[1:]  # ".1" … ".5": the hole is 6 mm plus this


def _fit_test() -> list[np.ndarray]:
    """A strip with holes 6.0 … 6.5 mm, each with its extra size written above it, and a 6 mm peg."""
    from shapely.geometry import Point, Polygon, box as sbox

    from ..additions import extrude

    strip = sbox(0, 0, 10 * len(FIT_GAUGE_GAPS) + 4, 18).difference(Polygon([(0, 0), (4, 0), (0, 4)]))  # notch: the 6.0 end
    for i, gap in enumerate(FIT_GAUGE_GAPS):
        strip = strip.difference(Point(7 + 10 * i, 6).buffer((6 + gap) / 2, 64))
    labels = [_label(_gap_text(g), 7 + 10 * i - 2.2, 11.5, 5, 4.5) for i, g in enumerate(FIT_GAUGE_GAPS)]
    return [extrude(strip, 0, 5), extrude(Point(0, 0).buffer(3, 64), 0, 12) + [30, 25, 0], *labels]


def _overhang_test(pitch: float = 12) -> list[np.ndarray]:
    """Blocks leaning 40° … 70°, each with its angle on top."""
    from shapely.geometry import Polygon

    out = []
    for i, a in enumerate(OVERHANG_TEST_DEG):
        out.append(_lying(Polygon([(0, 0), (8, 0), (8 + 15 * np.tan(np.radians(a)), 15), (0, 15)]), i * pitch, 8))
        out.append(_label(str(a), 1.0, i * pitch + 1.5, 15, 5))
    return out


def _bridge_test(pitch: float = 14) -> list[np.ndarray]:
    """Bridges 10 … 50 mm between pillars, each with its length on the left pillar (not on the bridge: no extra weight)."""
    from shapely.geometry import box as sbox

    out = []
    for i, span in enumerate(BRIDGE_TEST_MM):
        out.append(_lying(sbox(0, 0, 5, 10).union(sbox(5 + span, 0, 10 + span, 10)).union(sbox(0, 10, 10 + span, 11.2)), i * pitch, 8))
        out.append(_label(str(span), 0.5, i * pitch + 1.2, 11.2, 4, sideways=True))
    return out


def _dogbone() -> np.ndarray:
    """The layer strength dog-bone, lying flat."""
    from shapely.geometry import Point, Polygon

    from ..additions import extrude

    d = DOGBONE
    length = 2 * (d["end_l"] + d["round"]) + d["mid_l"]
    w, half, r, e = d["end_w"], d["mid_w"] / 2, d["round"], d["end_l"]
    # Top edge, then mirrored: the inside corners where the middle meets the ends are quarter rounds (no
    # corner for a crack to start at). Drawn point by point: a shrinking buffer can kill Pyodide's GEOS (T-268).
    a = np.linspace(0, np.pi / 2, 16)
    top = [(0, w / 2), (e, w / 2), *zip(e + r - r * np.cos(a), half + r - r * np.sin(a)),
           *zip(length - e - r + r * np.sin(a), half + r - r * np.cos(a)), (length - e, w / 2), (length, w / 2)]
    bone = Polygon([(x, w / 2 + y) for x, y in top] + [(x, w / 2 - y) for x, y in reversed(top)])
    for x in (d["end_l"] / 2, length - d["end_l"] / 2):
        bone = bone.difference(Point(x, w / 2).buffer(d["hole_d"] / 2, 64))
    return extrude(bone, 0, d["thick"])


def _creep_arm() -> np.ndarray:
    """A clamping tab, a straight beam, and a pointer whose height can be read against a fixed ruler."""
    from shapely.geometry import Point, Polygon

    from ..additions import extrude

    clamp = 30
    hole_x = clamp + CREEP_TEST["lever_mm"]
    centre = 12
    half = CREEP_TEST["width_mm"] / 2
    shoulder = [(0, 0), (22, 0), (clamp, centre - half), (hole_x + 3, centre - half), (hole_x + 8, centre - 1)]
    outline = Polygon(shoulder + [(x, 2 * centre - y) for x, y in reversed(shoulder)])
    hole = Point(hole_x, centre).buffer(CREEP_TEST["hole_mm"] / 2, 16)
    return extrude(outline.difference(hole), 0, CREEP_TEST["thickness_mm"])


def _warping_test(pitch: float = 20) -> list[np.ndarray]:
    """Bars 40 … 160 mm long, each with its length written at one end."""
    out = []
    f = np.array([[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]])
    for i, length in enumerate(WARPING_TEST_MM):
        v = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], float) * [length, 10, 3]
        out += [v[f] + [0, i * pitch, 0], _label(str(length), 2, i * pitch + 2, 3, 6)]
    return out


TEMP_TOWER_STEPS_C = (10, 5, 0, -5, -10, -15)  # the tower's blocks against the material's usual temperature, bottom first


TEMP_BLOCK_MM = 8.0


TEMP_BASE_MM = 1.0


IRONING_TEST_PCT = (6, 9, 12, 15, 18)  # ironing flows of the five steps (common starting points 8–15 %: thresholds looks)


IRONING_STEP_MM = 1.6


def _front_label(text: str, x: float, z: float, height: float) -> np.ndarray:
    """Raised digits on a front face (y = 0), reading from the front: the flat label turned up (a rotation about x)."""
    flat = _label(text, x, z, 0.0, height)  # drawn at y = z, standing on z = 0
    return np.stack([flat[..., 0], -flat[..., 2], flat[..., 1]], axis=-1)


def tower_temps(profile: dict | None = None) -> list[int]:
    """The temperature tower's block temperatures, bottom first: the material's usual nozzle temperature plus TEMP_TOWER_STEPS_C."""
    base = float(Profile.from_dict(profile).material["nozzle_temp_c"])
    return [int(round(base + d)) for d in TEMP_TOWER_STEPS_C]


def _temp_tower(temps: list[int]) -> list[np.ndarray]:
    """Six 20 × 10 × 8 mm blocks on a base, each with a 45° overhang on its right and its temperature on its front."""
    from shapely.geometry import Polygon, box as sbox

    from ..additions import extrude

    out = [extrude(sbox(-3, -3, 31, 13), 0, TEMP_BASE_MM)]
    for i, t in enumerate(temps):
        z0 = TEMP_BASE_MM + i * TEMP_BLOCK_MM
        out.append(_lying(Polygon([(0, z0), (20, z0), (28, z0 + TEMP_BLOCK_MM - 1), (0, z0 + TEMP_BLOCK_MM - 1)]), 0, 10))
        out.append(extrude(sbox(1, 1, 19, 9), z0 + TEMP_BLOCK_MM - 1.05, z0 + TEMP_BLOCK_MM))  # a narrow waist: the bands show
        out.append(_front_label(str(t), 4, z0 + 1.5, 4.5))
    return out


def _ironing_test() -> list[np.ndarray]:
    """Five 20 × 22 mm steps rising to the left, each flow written in the front strip of its top."""
    from shapely.geometry import box as sbox

    from ..additions import extrude

    out = []
    n = len(IRONING_TEST_PCT)
    for i, pct in enumerate(IRONING_TEST_PCT):
        top = IRONING_STEP_MM * (n - i)
        out.append(extrude(sbox(20 * i, 0, 20 * i + 20, 22), 0, top))
        out.append(_label(str(pct), 20 * i + 3, 1.5, top, 4))
    return out


def ironing_ranges() -> list[dict]:
    """The ironing test's flows as height ranges: each step's top layers get their own (export3mf ranges)."""
    n = len(IRONING_TEST_PCT)
    return [{"z0": round(IRONING_STEP_MM * (n - i) - IRONING_STEP_MM / 2, 3), "z1": round(IRONING_STEP_MM * (n - i) + 0.05, 3),
             "ironing_flow": f"{pct}%"} for i, pct in enumerate(IRONING_TEST_PCT)]


SAG_PINS_MM = (10, 17.5, 25, 32.5, 40)  # pin heights of the sag test: fewer pins, faster layers, higher up


SNAP_BAND_MM, SNAP_FANS = 8.0, (1.0, 0.8, 0.6, 0.4, 0.2)  # the snap test's fan steps, from the bottom


def _sag_test() -> list[np.ndarray]:
    """A 54 × 14 × 2 mm plate with five Ø3 mm pins, 10 … 40 mm tall, each height written in front of it."""
    from shapely.geometry import Point, box

    from ..additions import extrude

    out = [extrude(box(0, 0, 54, 14), 0, 2)]
    for i, height in enumerate(SAG_PINS_MM):
        x = 6 + 10.5 * i
        out += [extrude(Point(x, 10).buffer(1.5, 32), 1.9, height), _label(f"{height:g}", x - 4, 1.5, 2, 4)]
    return out


def _snap_test() -> list[np.ndarray]:
    """A wall 80 mm long, 1.2 mm thick and 40 mm tall on a 6 mm wide foot (so it stands while it prints)."""
    from shapely.geometry import box

    from ..additions import extrude

    return [extrude(box(0, -2.4, 80, 3.6), 0, 1.0), extrude(box(0, 0, 80, 1.2), 0.9, 40)]


def tuning_gcode(kind: str, data: bytes, profile: dict | None = None) -> dict:
    """The G-code for a tuning print (``kind`` "sag", "snap" or "temp"), from the user's own sliced test part
    (plain .gcode). Temp: each block of the temperature tower at its temperature (tower_temps for ``profile``'s
    material), see gcode.add_temperatures. Sag: the slicer's slowdown for layer cooling is switched off, so the layers really get
    faster with height. Snap: the part fan steps down every ``SNAP_BAND_MM`` (``SNAP_FANS``). Written and
    checked like optimize_gcode. Keep the file: tune_heat reads it with the result."""
    from ..gcode import parse
    from ..gcode_rewrite import check, rewrite
    from ..thermal_opt import Plan

    if kind == "temp":
        from ..gcode import add_temperatures

        temps = tower_temps(profile)
        text = data.decode("utf-8", "replace") if isinstance(data, (bytes, bytearray)) else str(data)
        new, where = add_temperatures(text, [(TEMP_BASE_MM + i * TEMP_BLOCK_MM, t) for i, t in enumerate(temps)])
        return {"data": new.encode(), "name": "temp_tower_tuning.gcode", "temps": temps, "bands": where}
    if kind not in ("sag", "snap"):
        raise ValueError("kind must be sag, snap or temp")
    tp = parse(data)
    plan = Plan.none(int(tp.layer.max()) + 1)
    if kind == "sag":
        plan.fast[:] = 100.0  # every layer-time slowdown undone (still never past the set speed or the flow cap)
        note = "Sag test: the slicer's slowdown for layer cooling is switched off."
    else:
        band = np.minimum((np.asarray(tp.layer_z) // SNAP_BAND_MM).astype(int), len(SNAP_FANS) - 1)
        plan.fan[2:] = np.asarray(SNAP_FANS)[band[2:len(plan.fan)]]
        note = "Snap test: the part fan steps " + " / ".join(f"{f:.0%}" for f in SNAP_FANS) + f" every {SNAP_BAND_MM:g} mm."
    new = rewrite(data, tp, plan, [note])
    problems = check(tp, new.text, new.toolpath)
    if problems:
        raise ValueError("The test G-code didn't pass its check: " + "; ".join(problems))
    return {"data": new.text.encode(), "name": f"{kind}_test_tuning.gcode", "minutes": [round(m, 1) for m in new.minutes]}


def tune_heat(kind: str, data: bytes, at_mm: float | None, profile: dict | None = None) -> dict:
    """The heat window value a tuning print gives, from its test G-code (``data``: tuning_gcode's file) and
    what the user saw: sag: ``at_mm`` where the tallest pin first drooped (None: none did); snap: ``at_mm``
    below which it broke easily (None: it held everywhere). The heat model says how hot the spots there were
    when covered (``sag_limit_c``) or how well they bonded (``min_bond``). Save it with the material (My
    materials) or pass it in the profile's ``heat_tuning``."""
    from ..gcode import parse
    from ..kinematics import timing
    from ..thermal import simulate

    p = Profile.from_dict(profile)
    tp = parse(data)
    if "Sag test" not in str(data[:4000]) and "Snap test" not in str(data[:4000]):
        raise ValueError("This isn't a test G-code from tuning_gcode: download that one, print it, then enter what you saw")
    h = simulate(tp, timing(tp), p)
    z = h.z
    if kind == "sag":
        top = h.column[np.argmax(z)]  # the tallest pin
        pin = (h.column == top) & np.isfinite(h.cover_c)
        if at_mm is None:
            value = float(np.nanmax(h.cover_c[pin])) + 5.0  # it held at the hottest: the limit is above that
            text = f"No pin drooped, so {p.material['name']} holds its shape at least up to about {value:.0f} °C on your printer."
        else:
            near = pin & (np.abs(z - float(at_mm)) <= 1.0)
            if not near.any():
                raise ValueError(f"The tallest pin has no layers near {at_mm} mm")
            value = float(np.median(h.cover_c[near]))
            text = f"The pin drooped from {at_mm:g} mm, where each layer was still about {value:.0f} °C when the next one went on."
        value = float(np.clip(value, 30, 300))
        return {"field": "sag_limit_c", "value": round(value), "text": text}
    if kind == "snap":
        wall = np.isfinite(h.bond) & (h.layer >= 2)
        if at_mm is None:
            value = float(np.nanpercentile(h.bond[wall], 10))
            text = "It held everywhere: even the coldest band bonds well enough on your printer."
        else:
            band = wall & (z >= float(at_mm)) & (z <= float(at_mm) + SNAP_BAND_MM)
            if not band.any():
                raise ValueError(f"The wall has no layers just above {at_mm} mm")
            value = float(np.median(h.bond[band]))
            text = f"Above {at_mm:g} mm it held: there the layers fused to about {value:.0%} of a full bond (a rough estimate)."
        return {"field": "min_bond", "value": round(float(np.clip(value, 0.02, 1.0)), 3), "text": text}
    raise ValueError("kind must be sag or snap")


def _all_tests() -> list[np.ndarray]:
    """The starter tests on one 170 × 175 mm plate (fits a 180 mm bed): warping bars at the front, the dog-bones and the
    fit gauge in the middle, overhang and bridge blocks at the back, the second dog-bone standing on its end."""
    bone = _dogbone()
    standing = bone[..., [1, 2, 0]]  # (x, y, z) -> (y, z, x): stood on its end; a cyclic swap keeps the winding
    return [*_warping_test(16),
            bone + [0, 64, 0], *(t + [104, 64, 0] for t in _fit_test()),
            *(t + [0, 104, 0] for t in _overhang_test(10)), *(t + [56, 104, 0] for t in _bridge_test(10)),
            standing + [124, 110, 0]]


def calibrate_bed_grip(longest_flat_mm: int, profile: dict | None = None) -> dict:
    """Estimate bed grip from the warping bars, using 0 if every bar lifted."""
    from .. import warp

    p = Profile.from_dict(profile)
    return {"enclosure": p.enclosure, **warp.calibrate_bed_grip(p, longest_flat_mm, _strength._shells(p))}


def calibration_part(kind: str, profile: dict | None = None) -> dict:
    """Test print for measuring the user's own material on their printer, as STL bytes, with ``how``
    (``CALIBRATION_HOW``: what to print and measure). Upright / flat break weight is the layer strength
    factor, the longest flat warping bar the warping length, the fit holes the fit gaps, the steepest
    clean block the max overhang, the longest straight bridge the max bridge. Each piece has its size
    written on it; ``all`` puts the starter tests on one plate (T-388). ``temp``: the temperature tower for ``profile``'s
    material (``temps``, bottom first); ``ironing``: the ironing steps, also as a 3MF (``3mf``) with each step's flow
    set for its height (OrcaSlicer / Bambu Studio height ranges). ``creep``: a clamped arm with a hanging weight
    and a pointed tip to measure its drop over time."""
    from ..mesh import to_binary_stl

    if kind == "creep":
        return {"stl": to_binary_stl(Mesh(_creep_arm())), "name": "creep_test.stl",
                "how": CALIBRATION_HOW[kind], "creep_test": CREEP_TEST}
    if kind == "temp":
        temps = tower_temps(profile)
        return {"stl": to_binary_stl(Mesh(np.concatenate(_temp_tower(temps)))), "name": "temp_tower.stl", "how": CALIBRATION_HOW[kind], "temps": temps}
    if kind == "ironing":
        from ..export3mf import export_3mf

        tris = np.concatenate(_ironing_test())
        lo, hi = tris.reshape(-1, 3).min(axis=0), tris.reshape(-1, 3).max(axis=0)
        mesh = Mesh(tris - [(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, lo[2]])  # export_3mf: centred, on z = 0
        t = load_data("thresholds")["looks"]
        settings = {"ironing_type": "top", "ironing_spacing": str(t["ironing_spacing_mm"]), "layer_height": "0.2",
                    "top_shell_layers": "4", "enable_support": "0"}
        threemf = export_3mf(mesh, "ironing_test", settings, ranges=ironing_ranges())
        return {"stl": to_binary_stl(mesh), "3mf": threemf, "name": "ironing_test.stl", "name_3mf": "ironing_test.3mf",
                "how": CALIBRATION_HOW[kind], "flows": list(IRONING_TEST_PCT)}

    build = {"fit": _fit_test, "overhang": _overhang_test, "bridge": _bridge_test, "layer_strength": lambda: [_dogbone()],
             "warping": _warping_test, "sag": _sag_test, "snap": _snap_test, "all": _all_tests}
    if kind not in build:
        raise ValueError(f"kind must be one of {CALIBRATION}")
    name = {"all": "all_tests.stl"}.get(kind, f"{kind}_test.stl")
    return {"stl": to_binary_stl(Mesh(np.concatenate(build[kind]()))), "name": name, "how": CALIBRATION_HOW[kind]}
