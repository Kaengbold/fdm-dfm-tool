"""When each line is really printed, and where the nozzle is at any moment (T-472).

The G-code's feed rates say how fast the printer may go, not how fast it does: it speeds up and slows
down at its acceleration, and slows for corners (Marlin jerk or junction deviation, Klipper
``square_corner_velocity``). Short lines and sharp corners therefore take longer than length / speed,
and the heat model (T-473) needs the real times: when a spot was covered, how long the nozzle and the
fan stayed over it.

A planner like the firmware's, in numpy: every move gets a trapezoid speed profile (speed up, cruise,
slow down) with look-ahead. The look-ahead is the usual two passes (each corner no faster than the next
moves can still slow down from, or the earlier ones could speed up to), written as running minimums so
it needs no Python loop over millions of moves. Big prints are planned a piece at a time, cut where the
nozzle stops anyway (retractions, pauses), so memory stays bounded. Bambu printers run a Marlin-based
firmware: they get the classic jerk model, checked against Bambu Studio's own estimate.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .gcode import MOTION_KEYS, Toolpath, _number

KLIPPER_SCV = 5.0  # mm/s, Klipper's default square_corner_velocity
KLIPPER_MCR = 0.5  # Klipper's default minimum_cruise_ratio
DEFAULT_ACCEL = 1500.0  # mm/s², when neither the file nor its settings say
DEFAULT_JERK = 10.0  # mm/s
DEFAULT_RETRACT_SPEED = 35.0  # mm/s
PIECE_MOVES = 200_000  # moves planned at once (~90 MB of temporary arrays)


@dataclass
class Timing:
    """Real times (s from the first move) of the toolpath's lines and travel moves, and every move's
    speed profile, in print order, for ``nozzle_at``."""

    t0: np.ndarray  # (N,) when each extruded line starts
    t1: np.ndarray  # (N,) when it ends
    travel_t0: np.ndarray  # (T,)
    travel_t1: np.ndarray  # (T,)
    total_s: float  # every move, pause and retraction
    model: str  # corner model used: "jerk", "junction_deviation" or "klipper"
    moves: dict  # in print order: "order" (index into lines, then travel), "t0", "v0" entry, "vp" peak, "v1" exit, "a"


def _setting(settings: dict, key: str, default: float) -> float:
    """First value (normal mode, first extruder) of a setting; 0 or missing: ``default``."""
    value = _number(settings.get(key), 0.0)
    return value if value > 0 else default


def _defaults(tp: Toolpath) -> tuple[dict, float, float]:
    """Motion limits from the slicer's machine settings, and the machine's acceleration caps for
    printing and travel (they clamp whatever the G-code asks for, as in the firmware)."""
    s = tp.settings
    max_extruding = _setting(s, "machine_max_acceleration_extruding", np.inf)
    max_travel = _setting(s, "machine_max_acceleration_travel", max_extruding)
    accel = _setting(s, "default_acceleration", DEFAULT_ACCEL if not np.isfinite(max_extruding) else max_extruding)
    return {
        "accel": accel, "travel_accel": _setting(s, "travel_acceleration", accel),
        "jerk_x": _setting(s, "machine_max_jerk_x", DEFAULT_JERK), "jerk_y": _setting(s, "machine_max_jerk_y", DEFAULT_JERK),
        "junction_deviation": _setting(s, "machine_max_junction_deviation", 0.0),
        "square_corner_velocity": KLIPPER_SCV, "accel_to_decel": 0.0, "minimum_cruise_ratio": KLIPPER_MCR,
        **{f"max_accel_{a}": _setting(s, f"machine_max_acceleration_{a}", np.inf) for a in "xyz"},
        **{f"max_speed_{a}": _setting(s, f"machine_max_speed_{a}", np.inf) for a in "xyz"},
    }, max_extruding, max_travel


def _corner_model(tp: Toolpath, defaults: dict) -> str:
    keys = set(tp.motion["key"].tolist())
    if tp.firmware == "klipper" or keys & {MOTION_KEYS.index(k) for k in ("square_corner_velocity", "accel_to_decel",
                                                                       "minimum_cruise_ratio")}:
        return "klipper"
    if MOTION_KEYS.index("junction_deviation") in keys or (tp.settings.get("gcode_flavor") == "marlin2"
                                                           and defaults["junction_deviation"] > 0):
        return "junction_deviation"
    return "jerk"


def _plan(p0, p1, feed, lim, model: str, stop: np.ndarray) -> tuple[np.ndarray, ...]:
    """Speed profiles of consecutive moves: entry, peak and exit speed, acceleration, duration.
    ``stop`` (n + 1 junctions, 0 before the first move): where the nozzle stands still; the first and
    last always are (the ends of the piece)."""
    n = len(p0)
    d = p1 - p0
    length = np.linalg.norm(d, axis=1)
    u = d / np.maximum(length, 1e-12)[:, None]
    absu = np.abs(u) + 1e-12
    speed = np.minimum(feed, np.min([lim[f"max_speed_{a}"] / absu[:, i] for i, a in enumerate("xyz")], axis=0))
    accel = np.minimum(lim["accel"], np.min([lim[f"max_accel_{a}"] / absu[:, i] for i, a in enumerate("xyz")], axis=0))
    take = lambda key: lim[key][1:] if np.ndim(lim[key]) else lim[key]

    # Corner speed² at junction i (between move i-1 and i).
    corner = np.full(n + 1, np.inf)
    if model == "jerk":  # each axis may change speed by at most its jerk at once
        jx, jy = lim["jerk_x"], lim["jerk_y"]
        corner[1:-1] = np.minimum(take("jerk_x") / (np.abs(u[1:, 0] - u[:-1, 0]) + 1e-12),
                                  take("jerk_y") / (np.abs(u[1:, 1] - u[:-1, 1]) + 1e-12)) ** 2
        safe = np.minimum(np.minimum(jx / absu[:, 0], jy / absu[:, 1]), speed) ** 2  # start from standing
    else:  # junction deviation: the corner speed of an arc through the corner
        a_j = np.minimum(accel[:-1], accel[1:])
        sin_half = np.minimum(np.sqrt(np.clip(0.5 * (1 + np.sum(u[1:] * u[:-1], axis=1)), 0, 1)), 1 - 1e-9)  # straight on: 1
        jd = take("square_corner_velocity") ** 2 * (np.sqrt(2) - 1) / a_j if model == "klipper" else take("junction_deviation")
        corner[1:-1] = a_j * jd * sin_half / (1 - sin_half)
        safe = np.zeros(n)
    corner[1:-1] = np.minimum(corner[1:-1], np.minimum(speed[:-1], speed[1:]) ** 2)
    corner[:-1] = np.where(stop[:-1], safe, corner[:-1])
    corner[-1] = safe[-1]

    # Look-ahead: v²[i] = min over k of (corner[k] + 2·a·(distance between i and k)), backwards then forwards.
    reach = np.concatenate([[0.0], np.cumsum(2 * accel * length)])
    back = reach[-1] - reach
    v2 = back + np.minimum.accumulate((corner - back)[::-1])[::-1]
    v2 = np.maximum(reach + np.minimum.accumulate(v2 - reach), 0.0)
    v_in, v_out = np.sqrt(v2[:-1]), np.sqrt(v2[1:])

    smooth = accel  # Klipper won't speed up a short move it must slow down from right away
    if model == "klipper":
        a2d = np.where(lim["accel_to_decel"] > 0, lim["accel_to_decel"], accel * (1 - lim["minimum_cruise_ratio"]))
        smooth = np.clip(a2d, 1e-9, accel)
    v_peak = np.sqrt(np.maximum(np.minimum(speed**2, smooth * length + (v_in**2 + v_out**2) / 2), np.maximum(v_in, v_out) ** 2))
    cruise = np.maximum(length - (2 * v_peak**2 - v_in**2 - v_out**2) / (2 * accel), 0.0)
    duration = (2 * v_peak - v_in - v_out) / accel + cruise / np.maximum(v_peak, 1e-9)
    return v_in, v_peak, v_out, accel, duration


def timing(tp: Toolpath) -> Timing:
    """Plan every move of ``tp`` like the firmware does and return the real times."""
    n_lines = len(tp.p0)
    order = np.argsort(np.concatenate([tp.t0, tp.travel["t0"]]), kind="stable").astype(np.int32)  # print order
    start = np.concatenate([tp.t0, tp.travel["t0"]])[order]
    n = len(order)
    defaults, max_extruding, max_travel = _defaults(tp)
    model = _corner_model(tp, defaults)

    # Where the nozzle stands: the start, the end, every retraction (not wipes: they move) and pause.
    extra = np.zeros(n + 1)  # seconds standing at each junction (before move i)
    r = tp.retracts
    still = ~r["moving"]
    r_speed = np.where(np.isfinite(r["speed"]) & (r["speed"] > 0), r["speed"], DEFAULT_RETRACT_SPEED)[still]
    for t_ev, seconds in ((r["t"][still], np.abs(np.nan_to_num(r["mm"][still])) / r_speed), (tp.dwells["t"], tp.dwells["s"])):
        np.add.at(extra, np.searchsorted(start, t_ev, side="left"), seconds)
    stop = extra > 0
    stop[0] = stop[n] = True
    stops = np.flatnonzero(stop)

    # The limits in force for each move: the last G-code command before it, else the settings.
    events = {}
    for k, key in enumerate(MOTION_KEYS):
        mine = tp.motion["key"] == k
        if mine.any():
            events[key] = (tp.motion["t"][mine], tp.motion["value"][mine])

    v0, vp, v1, acc = (np.empty(n, np.float32) for _ in range(4))  # kept for nozzle_at: compact
    duration = np.empty(n)
    i0 = 0
    lines_p0, lines_p1, travel = tp.p0, tp.p1, tp.travel
    while i0 < n:
        i1 = min(i0 + PIECE_MOVES, n)
        if i1 < n:  # end the piece where the nozzle stops anyway, if it does in the second half
            k = np.searchsorted(stops, i1, side="right") - 1
            i1 = int(stops[k]) if stops[k] > i0 + PIECE_MOVES // 2 else i1
            stop[i1] = True  # else (vase mode) stop here: a few ms too slow, but a speed profile it can follow
        idx = order[i0:i1]
        is_line = idx < n_lines
        li, ti = idx[is_line], idx[~is_line] - n_lines
        p0 = np.empty((len(idx), 3))
        p1 = np.empty((len(idx), 3))
        feed = np.empty(len(idx))
        p0[is_line], p1[is_line], feed[is_line] = lines_p0[li], lines_p1[li], tp.speed[li]
        p0[~is_line], p1[~is_line], feed[~is_line] = travel["p0"][ti], travel["p1"][ti], travel["speed"][ti]
        lim = dict(defaults)
        for key, (t_ev, values) in events.items():
            at = np.searchsorted(t_ev, start[i0:i1], side="right") - 1
            lim[key] = np.where(at >= 0, values[np.maximum(at, 0)], defaults[key])
        lim["accel"] = np.where(is_line, np.minimum(lim["accel"], max_extruding), np.minimum(lim["travel_accel"], max_travel))
        a, b, c, ac, du = _plan(p0, p1, feed, lim, model, stop[i0:i1 + 1])
        v0[i0:i1], vp[i0:i1], v1[i0:i1], acc[i0:i1], duration[i0:i1] = a, b, c, ac, du
        i0 = i1

    t_start = np.concatenate([[0.0], np.cumsum(duration[:-1])]) + np.cumsum(extra[:-1])
    t0, t1 = np.empty(n), np.empty(n)
    t0[order], t1[order] = t_start, t_start + duration
    return Timing(t0=t0[:n_lines], t1=t1[:n_lines], travel_t0=t0[n_lines:], travel_t1=t1[n_lines:],
                  total_s=float(t_start[-1] + duration[-1] + extra[-1]) if n else 0.0, model=model,
                  moves={"order": order, "t0": t_start, "v0": v0, "vp": vp, "v1": v1, "a": acc})


def nozzle_at(tp: Toolpath, tm: Timing, times) -> np.ndarray:
    """Where the nozzle is at each of ``times`` (s), following each move's speed profile; standing
    still between moves (retractions, pauses)."""
    m = tm.moves
    times = np.atleast_1d(np.asarray(times, float))
    i = np.clip(np.searchsorted(m["t0"], times, side="right") - 1, 0, len(m["t0"]) - 1)
    idx = m["order"][i]
    n_lines = len(tp.p0)
    line = idx < n_lines
    p0, p1 = np.empty((len(i), 3)), np.empty((len(i), 3))
    p0[line], p1[line] = tp.p0[idx[line]], tp.p1[idx[line]]
    p0[~line], p1[~line] = tp.travel["p0"][idx[~line] - n_lines], tp.travel["p1"][idx[~line] - n_lines]
    v0, vp, v1, a = (m[k][i].astype(float) for k in ("v0", "vp", "v1", "a"))
    length = np.linalg.norm(p1 - p0, axis=1)
    t_up, t_down = (vp - v0) / a, (vp - v1) / a
    t_cruise = np.maximum(length - (2 * vp**2 - v0**2 - v1**2) / (2 * a), 0) / np.maximum(vp, 1e-9)
    tau = np.maximum(times - m["t0"][i], 0.0)
    up = np.minimum(tau, t_up)
    down = np.clip(tau - t_up - t_cruise, 0, t_down)
    s = v0 * up + a * up**2 / 2 + vp * np.clip(tau - t_up, 0, t_cruise) + vp * down - a * down**2 / 2
    return p0 + (p1 - p0) * np.clip(s / np.maximum(length, 1e-12), 0, 1)[:, None]


def print_seconds(tp: Toolpath, tm: Timing) -> float:
    """From the first to the last line of the model (skirt, part and support; not the printer's own
    start and end code): what slicers call the model printing time."""
    own = np.array([f not in ("Custom", "(none)") for f in tp.features])[tp.feature]
    if not own.any():
        return tm.total_s
    return float(tm.t1[own].max() - tm.t0[own].min())
