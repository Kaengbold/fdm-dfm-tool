"""A cooling plan for the sliced print (T-480): where spots stay too hot, cool them (fan up ahead of time,
slow the layer that covers them); where the slicer slowed layers down for cooling they didn't need, undo it.

The heat model (thermal.py) takes tens of seconds per run, too slow to try every change. So the plan is
chosen with a simple cooling law fitted to two runs of it, then checked with a full run and corrected once:

1. Run the print as sliced, and once with the part fan far from the slicer's (100 %, or 20 % when the
   slicer already runs it high; same timing).
2. Every spot cools towards the air as ``T = T_air + (T_birth - T_air)·exp(-(a + b·h_fan)·t)`` until the next
   layer covers it: from its two covering temperatures, ``a`` (conduction into the part, radiation, still
   air) and ``b`` (what the fan adds) per spot. Its joint's bond moves with the temperature of the spot below
   it when it lands, linearly between the two runs.
3. Layer by layer from the bottom, pick the cheapest choice that keeps the spots it touches in their window
   (thermal.window): a fan value (it starts a few spin-ups before the layer and runs through the layer that
   covers it) and a speed for the layer's own lines (slower: the layer below has longer to cool; the
   slicer's own layer-time slowdown undone: the layer below is warmer when it is covered). Cost: print time
   plus, per spot outside its window, its importance weight × how far out it is (``thermal.yaml``
   ``optimizer``: what a degree or a share of bond is worth in seconds), plus a little per fan change.
4. Apply it to a copy of the toolpath, run the heat model, and correct once: layers still too hot get the
   next stronger choice, undone slowdowns that made a spot too hot are put back. Run again for the result.

Never touched: overhang, bridge, small-perimeter, flow-cap and unexplained slowdowns, layers 0–1, travel,
ironing and wipes, anything that isn't the part (supports, prime towers), and no line goes faster than its
feature's speed setting or the filament's flow cap. The total speed-up is capped (``max_speedup``).

The plan comes twice: for the G-code (fan and speeds per layer, for T-481) and for the user's own slicer
(speeds per height range and no fan changes, as export(cooling=...) writes them, T-479), each checked with
the heat model. All limits are estimates until real prints check them (T-477, T-482).
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np

from . import progress
from .gcode import BRIDGE, LAYER_TIME, NOT_SLOWED, OVERHANG, PART_KINDS, SMALL_PERIMETER, UNKNOWN, VOLUMETRIC_CAP, Toolpath, flow_cap
from .kinematics import timing
from .profile import Profile, load_data
from .thermal import Heat, _interp, constants, simulate, window

PROTECTED = (OVERHANG, BRIDGE, SMALL_PERIMETER, VOLUMETRIC_CAP, UNKNOWN)  # slowdowns that protect the print
FIRST_LAYER = 2  # layers 0 and 1 keep the slicer's settings (bed adhesion)
# Slicer feature names -> the speed a cooling range may set (recommend.COOLING_SPEEDS); others keep theirs.
ROLES = {"outer wall": "outer_wall", "external perimeter": "outer_wall", "inner wall": "inner_wall", "perimeter": "inner_wall",
         "sparse infill": "sparse_infill", "internal infill": "sparse_infill", "internal solid infill": "internal_solid_infill",
         "solid infill": "internal_solid_infill", "top surface": "top_surface", "top solid infill": "top_surface"}


def settings() -> dict:
    return load_data("thermal")["optimizer"]


@dataclass
class Plan:
    """Per layer k: the part fan while it prints and while the layer below it cools until k covers it (0..1,
    NaN: the slicer's), the speed factor for its adjustable lines (1: as sliced; below 1 slower), and how much
    of its layer-time slowdown is undone (``fast``: those lines go this many times faster, never past their
    speed setting; 1: none). ``park``: seconds the nozzle waits away from the part before layer k starts (a
    cooling park move: the layer below cools without the hot nozzle over it; in a slicer project a cooling
    tower does the same)."""

    fan: np.ndarray
    slow: np.ndarray
    fast: np.ndarray
    park: np.ndarray

    @classmethod
    def none(cls, layers: int) -> "Plan":
        return cls(np.full(layers, np.nan), np.ones(layers), np.ones(layers), np.zeros(layers))


def adjustable(tp: Toolpath) -> np.ndarray:
    """Lines the plan may slow down: the part's own, from layer 2 on, not slowed for a reason that protects
    the print, not ironing or a wipe, with a known speed setting."""
    names = np.array([f.lower() for f in tp.features] or [""])
    skip = np.array(["ironing" in n or "wipe" in n for n in names])
    return (np.isin(tp.kind, PART_KINDS) & (tp.layer >= FIRST_LAYER) & ~np.isin(tp.cause, PROTECTED)
            & ~skip[tp.feature] & np.isfinite(tp.set_speed))


def apply(tp: Toolpath, plan: Plan) -> Toolpath:
    """A copy of ``tp`` with the plan's speeds and part fan (fan commands rewritten where it sets one)."""
    cfg = settings()
    speed, cause = tp.speed.astype(float), tp.cause.copy()
    ok = adjustable(tp)
    length = np.linalg.norm(tp.p1 - tp.p0, axis=1)
    for k in np.flatnonzero(plan.fast > 1):
        lines = ok & (tp.layer == k) & (tp.cause == LAYER_TIME)
        speed[lines] = np.minimum(speed[lines] * plan.fast[k], tp.set_speed[lines])
        cause[lines & (speed >= tp.set_speed * 0.98)] = NOT_SLOWED
    for k in np.flatnonzero(plan.slow < 1):
        lines = ok & (tp.layer == k)
        speed[lines] = np.minimum(speed[lines] * plan.slow[k], tp.set_speed[lines])
        cause[lines & (cause == NOT_SLOWED)] = LAYER_TIME  # slowed for layer cooling, by the plan
    # Never more plastic per second than the filament's flow cap (the slicer held the set speed under it too).
    most = np.maximum(tp.speed, flow_cap(tp) * length / np.maximum(tp.volume, 1e-9))
    speed = np.where(length > 0, np.minimum(speed, most), tp.speed)

    # Part fan per line: the plan's value for layer k from the start of layer k - 1 (whose spots cool until k
    # covers them; a few spin-ups early) to the end of k; elsewhere the slicer's. Commands where it changes.
    fan = tp.part_fan.astype(float).copy()
    want = np.full(len(fan), np.nan)
    c_spin = float(load_data("thermal")["process"]["fan_spin_up_s"]["value"])
    first = np.searchsorted(tp.layer, np.arange(len(plan.fan) + 1))  # lines are in layer order
    for k in np.flatnonzero(np.isfinite(plan.fan)):
        if first[k] >= len(fan):
            continue
        lo = int(np.searchsorted(tp.t0, tp.t0[first[max(k - 1, 0)]] - float(cfg["fan_lead_spin_ups"]) * c_spin))
        hi = first[min(k + 1, len(plan.fan))]
        want[lo:hi] = np.fmax(want[lo:hi], plan.fan[k])
    fan = np.where(np.isfinite(want), want, fan)
    part = [i for i, name in enumerate(tp.fan_names) if name == "part"] or [0]
    keep = ~np.isin(tp.fans["fan"], part) | ~np.isfinite(want[np.minimum(tp.fans["line"], len(fan) - 1)])
    change = np.flatnonzero(np.diff(np.concatenate([[np.nan], fan])) != 0)
    change = change[np.isfinite(want[change]) | (np.concatenate([[False], np.isfinite(want[:-1])])[change])]
    fans = {key: np.concatenate([np.asarray(tp.fans[key])[keep], new])
            for key, new in (("line", change), ("t", tp.t0[change]), ("fan", np.full(len(change), part[0])), ("value", fan[change]))}
    order = np.argsort(fans["line"], kind="stable")
    fans = {key: np.asarray(v)[order].astype(np.asarray(tp.fans[key]).dtype if len(tp.fans[key]) else np.asarray(v).dtype)
            for key, v in fans.items()}
    travel, dwells = _parks(tp, plan)
    return replace(tp, speed=speed.astype(np.float32), cause=cause, part_fan=fan.astype(np.float32), fans=fans,
                   travel=travel, dwells=dwells)


PARK_MM = 20.0  # a park move goes this far past the part (in x), so the nozzle's heat doesn't reach it


def park_moves(tp: Toolpath, plan: Plan) -> list[dict]:
    """The plan's park moves: just before layer k's first line (``line``), from where the nozzle is (``here``)
    to beside the part (``away``), wait ``seconds``, and back."""
    first = np.searchsorted(tp.layer, np.arange(len(plan.park) + 1))
    ks = [k for k in np.flatnonzero(plan.park > 0) if first[k] < len(tp.t0)]
    if not ks:
        return []
    tr = tp.travel
    # Beside the part (its own lines, not a purge line at the bed's edge), where the nozzle already went.
    part = np.isin(tp.kind, PART_KINDS)
    xs = np.concatenate([tp.p0[:, 0], tp.p1[:, 0]])
    lo, hi = float(xs.min()), float(xs.max())
    p_lo, p_hi = float(tp.p0[part, 0].min()), float(tp.p0[part, 0].max())
    park_x = p_hi + PARK_MM if p_hi + PARK_MM <= hi else p_lo - PARK_MM if p_lo - PARK_MM >= lo else (hi if hi - p_hi > p_lo - lo else lo)
    starts = np.concatenate([tp.t0, tr["t0"]])
    ends = np.concatenate([tp.p1, tr["p1"]])
    out = []
    for k in ks:
        tf = float(tp.t0[first[k]])
        before = np.flatnonzero(starts < tf)
        here = ends[before[np.argmax(starts[before])]] if len(before) else tp.p0[first[k]]
        out.append({"layer": int(k), "line": int(first[k]), "here": np.asarray(here, float),
                    "away": np.array([park_x, here[1], here[2]], float), "seconds": float(plan.park[k])})
    return out


def _parks(tp: Toolpath, plan: Plan) -> tuple[dict, dict]:
    """Travel and pause events with the plan's park moves (kinematics.timing puts a pause at the move after it)."""
    moves = park_moves(tp, plan)
    if not moves:
        return tp.travel, tp.dwells
    tr = tp.travel
    speed = float(np.median(tr["speed"])) if len(tr["speed"]) else 150.0
    add = {key: [] for key in ("p0", "p1", "t0", "t1", "speed", "line")}
    pauses = {"line": [], "t": [], "s": []}
    for m in moves:
        tf = float(tp.t0[m["line"]])
        for a, b, t in ((m["here"], m["away"], tf - 2e-6), (m["away"], m["here"], tf - 1e-6)):
            add["p0"].append(a), add["p1"].append(b), add["t0"].append(t), add["t1"].append(t)
            add["speed"].append(speed), add["line"].append(m["line"])
        pauses["line"].append(m["line"]), pauses["t"].append(tf - 1.5e-6), pauses["s"].append(m["seconds"])
    travel = {key: np.concatenate([np.asarray(tr[key]), np.asarray(add[key], dtype=np.asarray(tr[key]).dtype if len(tr[key]) else None)])
              for key in add}
    travel.update({key: tr[key] for key in tr if key not in add})
    dwells = {key: np.concatenate([np.asarray(tp.dwells[key], float), np.asarray(pauses[key], float)]) for key in pauses}
    return travel, dwells


@dataclass
class _Law:
    """The fitted cooling law (see the module docstring)."""

    air: float
    a: np.ndarray  # (M,) 1/s
    b: np.ndarray  # (M,) 1/s per W/m²K of fan
    h_base: np.ndarray  # (M,) fan h while the node cooled as sliced
    below: np.ndarray  # (M,) index of the node under each one (-1: none)
    bond_base: np.ndarray
    bond_ref: np.ndarray
    cover_base: np.ndarray  # covering temperature of the node below (NaN: none)
    cover_ref: np.ndarray


def _below(h: Heat) -> np.ndarray:
    """Index of the node right under each node (same cell, layer below), -1 where there is none."""
    ij = np.round(h.xy / h.cell_mm).astype(np.int64)
    key = (h.layer.astype(np.int64) << 40) + ((ij[:, 0] + (1 << 19)) << 20) + (ij[:, 1] + (1 << 19))
    order = np.argsort(key)
    want = key - (1 << 40)
    at = np.minimum(np.searchsorted(key[order], want), len(key) - 1)
    return np.where(key[order][at] == want, order[at], -1)


def _fit(tp: Toolpath, base: Heat, ref: Heat, c: dict, ref_fan: float) -> _Law:
    """``ref``: the run with the part fan at ``ref_fan`` from layer 2 on."""
    air = float(c["chamber_c"])
    h_ref = float(_interp(c["h_fan"], 100.0 * ref_fan))
    layer_fan = np.zeros(int(base.layer.max()) + 2)
    np.maximum.at(layer_fan, tp.layer, tp.part_fan)  # the slicer's fan in each layer
    h_base = np.array([float(_interp(c["h_fan"], 100.0 * f)) for f in layer_fan])[np.minimum(base.layer + 1, len(layer_fan) - 1)]

    def rate(h: Heat) -> np.ndarray:
        with np.errstate(all="ignore"):
            return np.log((h.birth_c - air) / np.maximum(h.cover_c - air, 0.5)) / np.maximum(h.cover_s, 1e-3)

    k0, k1 = rate(base), rate(ref)
    dh = h_ref - h_base
    with np.errstate(all="ignore"):
        b = np.where(np.isfinite(k0 + k1) & (np.abs(dh) > 1e-6), np.maximum((k1 - k0) / dh, 0.0), 0.0)
    a = np.where(np.isfinite(k0), np.maximum(k0 - b * h_base, 0.0), 0.0)
    below = _below(base)
    pick = lambda arr: np.where(below >= 0, arr[np.maximum(below, 0)], np.nan)
    return _Law(air, a, b, h_base, below, base.bond, ref.bond, pick(base.cover_c), pick(ref.cover_c))


def _choices(tp: Toolpath, k: int, base_fan: float, ok: np.ndarray, allow_fan: bool, allow_undo: bool) -> list[tuple]:
    """(fan or NaN, slow factor, fast factor, park seconds) worth trying for layer ``k``."""
    cfg = settings()
    fans = [np.nan] + ([f for f in cfg["fan_steps"] if abs(f - base_fan) > 0.05] if allow_fan else [])
    out = [(f, 1.0, 1.0, 0.0) for f in fans]
    if ok[tp.layer == k].any():
        out += [(f, s, 1.0, 0.0) for s in cfg["slow_steps"] for f in fans]
        if allow_undo and (ok & (tp.layer == k) & (tp.cause == LAYER_TIME)).any():
            out += [(f, 1.0, x, 0.0) for x in cfg["fast_steps"] for f in fans]
    out += [(f, 1.0, 1.0, p) for p in cfg["park_steps"] for f in fans]
    return out


def _search(tp: Toolpath, h: Heat, law: _Law, win: dict, weight: np.ndarray, allow_fan: bool, allow_undo: bool,
            budget_s: float) -> Plan:
    """Layer by layer from the bottom: the cheapest choice by the fitted law (module docstring, step 3)."""
    cfg = settings()
    c_fan = load_data("thermal")["process"]["h_fan"]["points"]
    layers = int(tp.layer.max()) + 1
    plan = Plan.none(layers)
    ok = adjustable(tp)
    length = np.linalg.norm(tp.p1 - tp.p0, axis=1)
    base_time = length / np.maximum(tp.speed, 1e-3)
    undoable = ok & (tp.cause == LAYER_TIME)
    layer_fan = np.zeros(layers)
    np.maximum.at(layer_fan, tp.layer, tp.part_fan)
    hot_worth, bond_worth = float(cfg["seconds_per_degree"]), float(cfg["seconds_per_bond"])
    saved = 0.0
    by_layer = np.split(np.argsort(h.layer, kind="stable"), np.searchsorted(np.sort(h.layer), np.arange(1, layers)))
    for k in range(FIRST_LAYER, layers):
        under = by_layer[k - 1] if k - 1 < len(by_layer) else np.array([], int)  # covered by layer k
        own = by_layer[k] if k < len(by_layer) else np.array([], int)  # their joints form as layer k lands
        if not len(under) and not len(own):
            continue
        lines = tp.layer == k
        best, best_cost, best_saving = (np.nan, 1.0, 1.0, 0.0), np.inf, 0.0
        hot_groups = np.unique(h.column[under], return_inverse=True)[1] if len(under) else None
        own_groups = np.unique(h.column[own], return_inverse=True)[1] if len(own) else None
        cover = np.array([])
        for fan, slow, fast, park in _choices(tp, k, layer_fan[k], ok, allow_fan, allow_undo and saved < budget_s):
            t_new = np.where(ok & lines, base_time / slow, base_time)[lines]
            if fast > 1:
                quick = length[lines] / np.maximum(np.minimum(tp.speed[lines] * fast, tp.set_speed[lines]), 1e-3)
                t_new = np.where(undoable[lines], quick, t_new)
            extra = float(t_new.sum() - base_time[lines].sum()) + park * float(cfg["park_cost"])  # + slower, - faster
            if fast > 1 and saved - extra > budget_s:
                continue  # the total speed-up is capped
            h_fan = float(_interp(c_fan, 100.0 * fan)) if np.isfinite(fan) else None
            cost = extra + (0.5 if np.isfinite(fan) else 0.0)  # a fan change is not free (noise, wear)
            if len(under):
                # Anchored on the run as sliced: only the change the choice makes comes from the fitted law.
                hf = law.h_base[under] if h_fan is None else np.full(len(under), h_fan)
                t0 = h.cover_s[under]
                t = np.maximum(t0 + extra, 0.05)
                k0 = law.a[under] + law.b[under] * law.h_base[under]
                k1 = law.a[under] + law.b[under] * hf
                temp = law.air + (h.cover_c[under] - law.air) * np.exp(-(k1 * t - k0 * t0))
                over = np.nan_to_num(temp - win["limit_c"][under], nan=0.0)
                cost += hot_worth * _by_column(over, weight[under], hot_groups)
                cover = temp
            if len(own):
                below = law.below[own]
                has = below >= 0
                if has.any():
                    # The joint's bond follows the covering temperature of the node below, linearly between the runs.
                    pos = np.searchsorted(under, below[has]) if len(under) else np.zeros(has.sum(), int)
                    found = len(under) and (under[np.minimum(pos, len(under) - 1)] == below[has])
                    cov_new = np.where(found, cover[np.minimum(pos, len(under) - 1)] if len(under) else np.nan, law.cover_base[own][has])
                    c0, c1 = law.cover_base[own][has], law.cover_ref[own][has]
                    b0, b1 = law.bond_base[own][has], law.bond_ref[own][has]
                    with np.errstate(all="ignore"):
                        slope = np.where(np.abs(c0 - c1) > 0.5, (b0 - b1) / (c0 - c1), 0.0)
                    bond = np.clip(np.nan_to_num(b0 + slope * (cov_new - c0), nan=1.0), 0, 1)
                    cost += bond_worth * _by_column(win["min_bond"][own][has] - bond, weight[own][has], own_groups[has])
            if cost < best_cost - 1e-9:
                best, best_cost, best_saving = (fan, slow, fast, park), cost, -extra
        plan.fan[k], plan.slow[k], plan.fast[k], plan.park[k] = best
        if best[2] > 1:
            saved += best_saving
    return _smooth(plan)


def _by_column(short: np.ndarray, weight: np.ndarray, groups: np.ndarray) -> float:
    """How far a layer's spots are outside their window, as the window judges it: per column the median
    (a single cell does not count), times the column's mean weight, averaged over the columns."""
    out = []
    for g in range(int(groups.max()) + 1 if len(groups) else 0):
        sel = groups == g
        out.append(max(float(np.median(short[sel])), 0.0) * float(np.mean(weight[sel])))
    return float(np.mean(out)) if out else 0.0


def _smooth(plan: Plan) -> Plan:
    """Fewer fan changes and no sudden speed jumps on the outer walls between layers: a fan value holds for
    at least ``min_fan_layers`` layers (the higher one wins), and a layer's speed factor is at most
    ``max_speed_step`` away from its neighbours'."""
    cfg = settings()
    fan, slow = plan.fan.copy(), plan.slow.copy()
    run = int(cfg["min_fan_layers"])
    for k in np.flatnonzero(np.isfinite(plan.fan)):
        fan[k:k + run] = np.fmax(fan[k:k + run], plan.fan[k])
    step = float(cfg["max_speed_step"])  # a neighbour is at most 1 / step faster: the change ramps in and out
    for k in range(1, len(slow)):
        slow[k] = min(slow[k], slow[k - 1] / step)
    for k in range(len(slow) - 2, -1, -1):
        slow[k] = min(slow[k], slow[k + 1] / step)
    slow = np.where(slow > 0.97, 1.0, np.minimum(slow, 1.0))
    return Plan(fan, slow, plan.fast.copy(), plan.park.copy())


def _same(flags: np.ndarray, h: Heat, base: Heat) -> np.ndarray:
    """``flags`` of the run as sliced, carried to the nodes of ``h`` (same layer and cell; the plan only changes
    timing and fan, so the nodes match one to one)."""
    if len(h.layer) == len(base.layer):
        return flags
    key = lambda x: (x.layer.astype(np.int64) << 32) + (np.round(x.xy[:, 0] / x.cell_mm).astype(np.int64) << 16) + np.round(x.xy[:, 1] / x.cell_mm).astype(np.int64)
    kb, kh = key(base), key(h)
    order = np.argsort(kb)
    at = np.minimum(np.searchsorted(kb[order], kh), len(kb) - 1)
    return np.where(kb[order][at] == kh, flags[order][at], False)


def _score(h: Heat, win: dict, weight: np.ndarray) -> dict:
    hot, cold = win["too_hot"], win["too_cold"] & ~win["needless"]
    return {"too_hot": int(hot.sum()), "too_cold": int((win["too_cold"]).sum()), "needless": int(win["needless"].sum()),
            "weighted_hot": float(np.sum(weight[hot])), "weighted_cold": float(np.sum(weight[win["too_cold"]])),
            "bond_median": round(float(np.nanmedian(h.bond)), 3) if np.isfinite(h.bond).any() else None}


def optimize(tp: Toolpath, profile: Profile, weight_at=None, intents_at=None, tier: str = "quick") -> dict:
    """The cooling plan for ``tp`` (module docstring). ``weight_at(heat)`` -> importance weight per node and
    ``intents_at(heat)`` -> design-intent masks (thermal.window); both optional. Returns the plans, the print
    time and the window before and after, and the heat results (``heat``: before, gcode, slicer)."""
    cfg = settings()
    c = constants(profile, tp.settings)
    stages = iter(np.linspace(0, 1, 7))

    def run(path: Toolpath, snapshots: bool = False) -> tuple[Heat, float]:
        lo = next(stages)
        with progress.stage(lo, lo + 1 / 6):
            tm = timing(path)
            return simulate(path, tm, profile, tier, snapshots=snapshots), tm.total_s

    def judge(h: Heat) -> tuple[dict, np.ndarray]:
        w = np.ones(len(h.layer)) * 0.5 if weight_at is None else np.asarray(weight_at(h), float)
        return window(h, profile, None if intents_at is None else intents_at(h), w), w

    layers = int(tp.layer.max()) + 1
    base, t_base = run(tp, snapshots=True)  # the replay (T-588) shows the print as sliced and with the G-code plan
    ref = Plan.none(layers)
    ref_fan = 0.2 if float(np.median(tp.part_fan[tp.layer >= FIRST_LAYER])) > 0.6 else 1.0
    ref.fan[FIRST_LAYER:] = ref_fan
    reference, _ = run(apply(tp, ref))
    law = _fit(tp, base, reference, c, ref_fan)
    win0, weight = judge(base)
    budget = float(cfg["max_speedup"]) * t_base

    def plan_and_check(allow_fan: bool, allow_undo: bool) -> tuple[Plan, Heat, float, dict, np.ndarray]:
        plan = _search(tp, base, law, win0, weight, allow_fan, allow_undo, budget)
        h, t = run(apply(tp, plan), snapshots=allow_fan)
        win, w = judge(h)
        # One correction, layer k by its spots: the layer below it still too hot -> the next stronger choice on
        # k; k's own joints made too cold by the plan -> one step back; both -> whichever weighs more.
        layer_w = lambda mask: np.bincount(h.layer[mask].astype(np.intp), w[mask], minlength=layers + 1)
        hot_below = np.concatenate([[0.0], layer_w(win["too_hot"])[:layers]])  # index k: hot spots in layer k - 1
        new_cold = layer_w(win["too_cold"] & ~_same(win0["too_cold"], h, base))[:layers]
        changed = False
        for k in range(FIRST_LAYER, layers):
            if hot_below[k] > new_cold[k] and hot_below[k] > 0:
                if plan.fast[k] > 1:
                    plan.fast[k] = 1.0
                elif allow_fan and not plan.fan[k] >= 1.0:
                    plan.fan[k] = 1.0
                elif plan.park[k] < max(cfg["park_steps"]):
                    # Slowing more keeps the hot nozzle over a small spot longer: wait away from it instead.
                    plan.park[k] = min([p for p in cfg["park_steps"] if p > plan.park[k]])
                else:
                    continue
            elif new_cold[k] > 0:
                if plan.park[k] > 0:
                    plan.park[k] = max([p for p in cfg["park_steps"] if p < plan.park[k]] or [0.0])
                elif plan.slow[k] < 1:
                    plan.slow[k] = min([x for x in cfg["slow_steps"] if x > plan.slow[k] + 1e-9] or [1.0])
                elif np.isfinite(plan.fan[k]):
                    plan.fan[k] = np.nan
                else:
                    continue
            else:
                continue
            changed = True
        if changed:
            plan = _smooth(plan)
            h, t = run(apply(tp, plan), snapshots=allow_fan)
            win, w = judge(h)
        else:
            next(stages)
        return plan, h, t, win, w

    gplan, gheat, t_g, gwin, _ = plan_and_check(allow_fan=True, allow_undo=True)
    splan, sheat, t_s, swin, _ = plan_and_check(allow_fan=False, allow_undo=False)  # what a slicer project can carry
    return {
        "before": {"print_s": round(t_base, 1), **_score(base, win0, weight)},
        "gcode": {"print_s": round(t_g, 1), **_score(gheat, gwin, weight), "plan": _plan_rows(tp, gplan)},
        "slicer": {"print_s": round(t_s, 1), **_score(sheat, swin, weight), "cooling": slicer_plan(tp, splan)},
        "heat": {"before": base, "gcode": gheat, "slicer": sheat}, "windows": {"before": win0, "gcode": gwin, "slicer": swin},
        "plans": {"gcode": gplan, "slicer": splan},
    }


def _plan_rows(tp: Toolpath, plan: Plan) -> list[dict]:
    """The G-code plan as rows of layers in a row with the same choice (only those with a change)."""
    rows = []
    for k in range(len(plan.fan)):
        choice = (None if not np.isfinite(plan.fan[k]) else round(float(plan.fan[k]), 2), round(float(plan.slow[k]), 2),
                  round(float(plan.fast[k]), 2), round(float(plan.park[k]), 1))
        if choice == (None, 1.0, 1.0, 0.0):
            continue
        z = float(tp.layer_z[k]) if k < len(tp.layer_z) else None
        if rows and rows[-1]["choice"] == choice and rows[-1]["last"] == k - 1:
            rows[-1].update(last=k, z1=z)
        else:
            rows.append({"first": k, "last": k, "z0": z, "z1": z, "choice": choice})
    return [{"layers": [r["first"], r["last"]], "z_mm": [r["z0"], r["z1"]], "fan": r["choice"][0], "slower": r["choice"][1],
             "faster": r["choice"][2], "park_s": r["choice"][3]} for r in rows]


TOWER_MM = (8.0, 25.0)  # cooling tower diameters the slicer plan picks from (bigger ones waste too much plastic)
TOWER_DENSITY = 0.15  # the slicer's usual sparse infill inside it


def _tower(tp: Toolpath, plan: Plan) -> dict | None:
    """A cooling tower that keeps the nozzle away from the part about as long as the plan's longest park,
    up to the last parked layer: its walls (two loops) and sparse infill at the slicer's set speeds."""
    if not (plan.park > 0).any():
        return None
    wall = np.nanmedian(tp.set_speed[np.isin(tp.kind, PART_KINDS)]) if np.isfinite(tp.set_speed).any() else 60.0
    width = 0.45
    for d in np.arange(*TOWER_MM, 1.0):
        seconds = 2 * np.pi * d / wall + (np.pi * d * d / 4) * TOWER_DENSITY / width / wall
        if seconds >= float(plan.park.max()):
            break
    top = int(np.flatnonzero(plan.park > 0).max())
    return {"height": round(float(tp.layer_z[min(top, len(tp.layer_z) - 1)]), 2), "diameter": float(d)}


def slicer_plan(tp: Toolpath, plan: Plan) -> dict:
    """The plan as export(cooling=...) takes it: per run of slowed layers, each kind of line's set speed ×
    the factor (the slowest in the run, mm/s); park moves become a cooling tower (``_tower``)."""
    names = [ROLES.get(f.lower()) for f in tp.features]
    role = np.array([names[i] if i < len(names) else None for i in tp.feature], dtype=object) if len(names) else np.full(len(tp.layer), None)
    ok = adjustable(tp)
    ranges = []
    k, layers = 0, len(plan.slow)
    while k < layers:
        if plan.slow[k] >= 1:
            k += 1
            continue
        end = k
        while end + 1 < layers and abs(plan.slow[end + 1] - plan.slow[k]) < 1e-9:
            end += 1
        lines = ok & (tp.layer >= k) & (tp.layer <= end)
        speeds = {}
        for r in set(role[lines]) - {None}:
            sel = lines & (role == r)
            speeds[r] = round(float(np.min(tp.set_speed[sel]) * plan.slow[k]), 1)
        if speeds:
            lh = float(np.median(tp.layer_height[tp.layer == k])) if (tp.layer == k).any() else 0.2
            ranges.append({"z0": round(float(tp.layer_z[k]) - lh, 3), "z1": round(float(tp.layer_z[min(end, len(tp.layer_z) - 1)]), 3), "speeds": speeds})
        k = end + 1
    tower = _tower(tp, plan)
    return {"ranges": ranges, **({"tower": tower} if tower else {})}
