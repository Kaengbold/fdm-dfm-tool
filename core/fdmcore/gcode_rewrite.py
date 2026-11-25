"""The cooling plan written into the user's own G-code (T-481, option B: opt-in; the original stays theirs).

The plan changes feed rates (the F of the printed lines the plan slows or speeds up) and cooling
fans (one command where a setting changes, in the firmware's own form). Park moves add a retract, a travel
beside the part, a pause and the way back. Positions, extrusion amounts and temperatures stay as they are.
The progress lines (M73) are recomputed, and a header says what changed.

Before the file is handed out it is checked (``check``): read back, it must print the same paths with the
same plastic per layer inside the same bounding box, no speed above the machine's limits or the
filament's flow cap, and a heat simulation of it must leave no spot newly outside its window. If any
check fails the file isn't offered, and the reason says why.

Output is plain ``.gcode``, including from a Prusa binary input. A ``.gcode.3mf`` needs repacking (plate G-code, md5, slice_info.config),
which waits for a test on a real printer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np

from .gcode import Toolpath, _line_blocks, _words, fan_levels, flow_cap, parse
from .kinematics import _defaults, timing
from .thermal_opt import Plan, apply, aux_schedule, park_feeds, park_moves

PA_TOLERANCE = 0.25  # with adaptive pressure advance (no table to redo it), speeds change at most this much
_WORD = re.compile(r"\b([A-Z])([-+]?\d*\.?\d+)")


@dataclass
class Rewrite:
    text: str
    toolpath: Toolpath  # what the plan made of the original (speeds, fan, parks), for the checks
    changed_lines: int
    fan_commands: int
    parks: int
    clamped: bool  # speed changes were held to ±PA_TOLERANCE (adaptive pressure advance)
    minutes: tuple[float, float]  # print time before, after


def _adaptive_pa(tp: Toolpath) -> bool:
    return len(np.unique(np.round(tp.pressure_advance["value"], 4))) > 2


def _fan_command(tp: Toolpath, value: float) -> str:
    """Set the part fan, as this firmware wants it (Bambu calls it P1; Marlin, Klipper and RRF take S0–255)."""
    s = int(round(min(max(value, 0.0), 1.0) * 255))
    return f"M106 P1 S{s}" if tp.firmware == "bambu" else f"M106 S{s}"


def _is_part_fan(line: str, tp: Toolpath) -> bool:
    cmd = line.split(";", 1)[0].split()
    if not cmd or cmd[0].upper() not in ("M106", "M107"):
        return False
    words = dict(_WORD.findall(" ".join(cmd[1:]).upper()))
    p = int(float(words.get("P", 0)))
    return p == 0 or (p == 1 and tp.firmware == "bambu")


def _set_feed(line: str, feed: float) -> str:
    """``line`` (a G0–G3 move) with its F word set to ``feed`` mm/min (added when it has none)."""
    code, sep, comment = line.partition(";")
    text = f"F{feed:.0f}" if feed >= 100 else f"F{feed:.1f}"
    if re.search(r"(?<=\s)F[-+]?\d*\.?\d+", code):
        code = re.sub(r"(?<=\s)F[-+]?\d*\.?\d+", text, code, count=1)
    else:
        code = code.rstrip() + " " + text + (" " if sep else "")
    return code + sep + comment


def rewrite(data, tp: Toolpath, plan: Plan, note: list[str] | None = None) -> Rewrite:
    """``data`` (the plain G-code ``tp`` was read from) with ``plan`` written in (module docstring)."""
    if tp.src is None:
        raise ValueError("The toolpath doesn't know its text lines: read the G-code again")
    new = apply(tp, plan)
    speed = new.speed.astype(float)
    clamped = _adaptive_pa(tp) and bool(np.any(np.abs(speed / np.maximum(tp.speed, 1e-6) - 1) > PA_TOLERANCE + 1e-6))
    if clamped:  # adaptive pressure advance was tuned for the slicer's speeds: stay near them
        speed = np.clip(speed, tp.speed * (1 - PA_TOLERANCE), tp.speed * (1 + PA_TOLERANCE))
        new.speed[:] = speed
    changed = np.abs(speed - tp.speed) > 1e-3
    fan = new.part_fan.astype(float)
    change_part = np.isfinite(plan.fan).any()
    aux_override, aux = aux_schedule(tp, plan)
    t_old, t_new = timing(tp), timing(new)

    # Per text line: the first printed line it holds (lines from one arc share their text line).
    first_of = {}
    for i in np.flatnonzero(np.r_[True, np.diff(tp.src) != 0]):
        first_of[int(tp.src[i])] = int(i)
    lo_src, hi_src = int(tp.src[0]), int(tp.src[-1])
    parks = {m["line"]: m for m in park_moves(tp, plan)}
    retract, retract_f, travel_f = park_feeds(tp)
    progress = _progress(t_old, t_new, first_of)

    out: list[str] = []
    e_rel, e_abs = False, 0.0
    orig_feed = out_feed = None  # the modal F in the original and in the output
    fan_now = None
    n_fan = 0
    src = -1
    newline = "\n"
    for block in _line_blocks(data):
        for line in block:
            src += 1
            head = line.split(";", 1)[0].strip().upper()
            cmd = head.split(None, 1)[0] if head else ""
            i = first_of.get(src)
            if i is not None:
                if i in aux:
                    out.append(f"M106 P2 S{round(aux[i] * 255)}")
                    n_fan += 1
                if i in parks:  # a cooling park move before this layer starts
                    m = parks[i]
                    back = (f"G1 E{retract:.3f} F{retract_f:.0f}" if e_rel else f"G1 E{e_abs:.5f} F{retract_f:.0f}")
                    out += [f"; cooling park move: {m['seconds']:g} s beside the part",
                            f"G1 E{-retract:.3f} F{retract_f:.0f}" if e_rel else f"G1 E{e_abs - retract:.5f} F{retract_f:.0f}",
                            f"G1 X{m['away'][0]:.3f} Y{m['away'][1]:.3f} F{travel_f:.0f}", f"G4 S{m['seconds']:g}",
                            f"G1 X{m['here'][0]:.3f} Y{m['here'][1]:.3f} F{travel_f:.0f}", back]
                    out_feed = None  # the moves above set their own F: the next move sets it again
                if change_part and (fan_now is None or abs(fan[i] - fan_now) > 1e-3):
                    out.append(_fan_command(tp, fan[i]))
                    fan_now, n_fan = fan[i], n_fan + 1
            if cmd in ("M106", "M107"):
                if change_part and lo_src <= src <= hi_src and _is_part_fan(line, tp):
                    continue  # the part fan comes from the plan's per-line values (written above)
                if aux and _words(head).get("P") == 2 and aux_override[np.searchsorted(tp.src, src)]:
                    continue  # the auxiliary fan follows the plan over this span
            if cmd == "M73" and (now := progress(src)) is not None:
                pct, scale = now
                line = re.sub(r"\b([PQ])\d+", lambda m: f"{m.group(1)}{pct}", line)
                line = re.sub(r"\b([RS])(\d+)", lambda m: f"{m.group(1)}{round(int(m.group(2)) * scale)}", line)
            if cmd in ("M82", "M83"):
                e_rel = cmd == "M83"
            elif cmd == "G92":
                w = dict(_WORD.findall(head))
                if "E" in w:
                    e_abs = float(w["E"])
            if cmd in ("G0", "G1", "G2", "G3"):
                w = dict(_WORD.findall(head[len(cmd):]))
                if "E" in w:
                    e_abs = e_abs + float(w["E"]) if e_rel else float(w["E"])
                if "F" in w:
                    orig_feed = float(w["F"])
                want = speed[i] * 60 if i is not None and changed[i] else orig_feed
                if want is not None and ("F" in w or out_feed is None or abs(want - out_feed) > 0.05):
                    line = _set_feed(line, want)
                    out_feed = want
            out.append(line)
            if src == hi_src and len(tp.src) in aux:
                out.append(f"M106 P2 S{round(aux[len(tp.src)] * 255)}")
                n_fan += 1
    old_min, new_min = t_old.total_s / 60, t_new.total_s / 60
    header = ["; FDM DFM Tool cooling plan (opt-in). The original G-code is unchanged on your disk.",
              f"; Estimated print time {old_min:.1f} min -> {new_min:.1f} min.",
              f"; Changed: the speed of {int(changed.sum())} printed lines, {n_fan} cooling fan commands, {len(parks)} cooling park moves."
              + (" Speed changes kept within 25 % (adaptive pressure advance)." if clamped else ""),
              *[f"; {x}" for x in note or []]]
    # After the slicer's own header block when there is one (printers read it for the time and thumbnails).
    at = next((k + 1 for k, line in enumerate(out[:200]) if "HEADER_BLOCK_END" in line), 0)
    text = newline.join(out[:at] + header + out[at:]) + newline
    return Rewrite(text, new, int(changed.sum()), n_fan, len(parks), clamped, (old_min, new_min))


def _progress(t_old, t_new, first_of: dict):
    """For an M73 line (by text line): the new percent done, and how much the time left changed (its minutes
    left are scaled by it, so the slicer's own estimate, heat-up included, stays the base). None before the
    first printed line: the start G-code doesn't change."""
    srcs = np.array(sorted(first_of))
    firsts = np.array([first_of[k] for k in srcs])
    total_old, total_new = float(t_old.total_s), float(t_new.total_s)

    def at(src: int):
        k = int(np.searchsorted(srcs, src))
        if k == 0:
            return None
        if k >= len(srcs):
            return 100, 0.0
        left_old = total_old - float(t_old.t0[firsts[k]])
        left_new = total_new - float(t_new.t0[firsts[k]])
        return int(round(100 * (1 - left_new / total_new))), (left_new / left_old if left_old > 1 else 1.0)

    return at


def check(original: Toolpath, text: str, plan_toolpath: Toolpath) -> list[str]:
    """What's wrong with the rewritten G-code, in plain words (empty: fine to offer). The heat check is
    done by the caller (it needs the profile and the importance weights)."""
    problems = []
    try:
        back = parse(text, original.settings)
    except ValueError as e:
        return [f"It can't be read back: {e}"]
    if len(back.p0) != len(original.p0) or not (np.allclose(back.p0, original.p0, atol=1e-3) and np.allclose(back.p1, original.p1, atol=1e-3)):
        return ["The printed paths changed"]
    layers = int(original.layer.max()) + 1
    e_old = np.bincount(original.layer, original.volume, minlength=layers)
    e_new = np.bincount(back.layer, back.volume, minlength=layers)
    if not np.allclose(e_old, e_new, rtol=1e-4, atol=1e-3):
        problems.append("The plastic per layer changed")
    lo_o, hi_o = original.p0.min(axis=0), original.p0.max(axis=0)
    lo_b, hi_b = back.p0.min(axis=0), back.p0.max(axis=0)
    if not (np.allclose(lo_o, lo_b, atol=1e-3) and np.allclose(hi_o, hi_b, atol=1e-3)):
        problems.append("The part's size or place changed")
    limits = _defaults(original)[0]
    d = back.p1 - back.p0
    length = np.maximum(np.linalg.norm(d, axis=1), 1e-9)
    for k, axis in enumerate("xy"):
        v = np.abs(d[:, k]) / length * back.speed
        if np.any(v > limits.get(f"max_speed_{axis}", np.inf) * 1.001):
            problems.append(f"A speed is above the printer's {axis.upper()} limit")
    cap = flow_cap(original)
    flow = back.volume / (length / np.maximum(back.speed, 1e-6))
    before = original.volume / (length / np.maximum(original.speed, 1e-6))
    if np.any((flow > cap * 1.02) & (flow > before * 1.001)):
        problems.append(f"Some lines would need more plastic per second than the filament can melt ({float(np.min(cap)):g} mm³/s)")
    if np.any(np.abs(back.speed - plan_toolpath.speed) > 0.05 * np.maximum(plan_toolpath.speed, 1)):
        problems.append("The speeds read back don't match the plan")
    if "P2" in plan_toolpath.fan_names and not np.allclose(fan_levels(back, "P2"), fan_levels(plan_toolpath, "P2"), atol=1 / 255):
        problems.append("The auxiliary fan settings read back don't match the plan")
    return problems
