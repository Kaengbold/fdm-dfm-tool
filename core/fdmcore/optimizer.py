"""The optimizer behind the new UI's Analyse and Options steps (T-522, plan research/redesign-2026-11/PLAN-COMBINED.md
§10.2, §10.3, §11, §14, §16).

For the loaded part it searches orientation × material × nozzle × layer height × walls/top/infill and returns 2–4
options that really differ, each verified with the full direction-dependent strength check at its own pose and
settings. Built from the engine's existing pieces (orientation scoring, sliced support estimate, the greedy shell
search ``api._climb``), so a number here means the same as everywhere else in the app.

Rules from the owner (§16):
- Safety targets are floors: Strong ×5, Balanced ×3, Fast ×2, Light ×1.5; a load that stays on raises it to ×3.
  Nothing below the floor is offered as meeting it; if nothing reaches it, the best reachable comes back marked
  ``below_target`` with the ways out (decision 2: usable after a warning).
- Quality and "no loads" never run a strength solve (§14).
- Nozzles only from the user's ticked list; one bigger than 0.4 only if the part still prints with it and the gain
  is significant (decision 7, no size cut-off).
- "The design" only: the recipe stays at the baseline; orientation is searched (decision 4). Design additions at
  weak spots are T-523 (Complete depth) and are listed in ``skipped`` until then.

Pyodide-safe: numpy + the engine only.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import numpy as np

from . import progress
from .profile import Profile, presets

GOAL_FLOOR = {"strong": 5.0, "balanced": 3.0, "fast": 2.0, "light": 1.5, "quality": None}
SUSTAINED_FLOOR = 3.0  # a load that never lets go creeps: at least ×3 whatever the goal (service.CONSTANT_LOAD_SAFETY)
GOALS = tuple(GOAL_FLOOR)
DEPTHS = ("fast", "extended", "complete")
# How wide each depth searches. Complete = Extended + design additions (T-523).
DEPTH_POSES = {"fast": 3, "extended": 6, "complete": 6}
REFINE_POSES = {"fast": 1, "extended": 3, "complete": 3}  # poses that get the full recipe search
NOZZLE_GAIN = 0.15  # a bigger nozzle must save ≥ 15 % time or add ≥ 15 % safety over 0.4 to be offered (§10.3)
DISTINCT_TIME, DISTINCT_GRAMS = 0.10, 0.15  # options must differ this much, or in material / nozzle (§10.3 step 8)
MAX_OPTIONS = 4
WARP_COST = {"high": 0.3, "medium": 0.1}  # harder-to-print materials need a real gain to be recommended
OWNED_BONUS = 0.15
OWN_SEARCH_MATERIALS = 2  # Extended: materials (owned first, else PLA / PETG) that get their own recipe search
SUPPORT_FILL = 0.25  # as api.SUPPORT_FILL: support is printed sparse
CHANGES = ("slicer", "design", "both")
# What the "What will it touch?" chips check in the material data (T-520 fields; outdoors = UV).
TOUCH_FIELD = {"water": "water", "abrasion": "abrasion", "oil_fuel": "oil_fuel", "solvent": "solvent", "skin": "skin"}
TOUCH_WORDS = {"water": "water", "abrasion": "scraping", "oil_fuel": "oil and fuel", "solvent": "cleaners", "skin": "skin",
               "outdoors": "sun and rain", "food": "food"}


@dataclass
class Pose:
    up: np.ndarray
    label: str
    ease: float  # orientation score (lower = easier to print)
    support_mm3: float  # rough support volume from the orientation score
    height: float
    safety_hint: float | None = None  # isotropic one-solve safety (only for picking candidates)
    tags: set = field(default_factory=set)  # which shortlists it came from


def target_for(goal: str, constant_load: bool) -> float | None:
    floor = GOAL_FLOOR[goal]
    if floor is None:
        return None
    return max(floor, SUSTAINED_FLOOR) if constant_load else floor


def pose_label(up) -> str:
    """Plain words for a build direction in the part's own axes ("As loaded", "−X side down", "Tilted")."""
    up = np.asarray(up, float) / np.linalg.norm(up)
    if up[2] > 0.999:
        return "As loaded"
    if up[2] < -0.999:
        return "Upside down"
    k = int(np.argmax(np.abs(up)))
    if abs(up[k]) > 0.98:
        return f"{'−' if up[k] > 0 else '+'}{'XYZ'[k]} side down"
    return "Tilted"


def allowed_materials(base: Profile, env: dict, lock: str | None, allowed: list[str] | None) -> tuple[list[str], list[dict], dict[str, list[str]]]:
    """Materials that fit the environment and the printer: (ids, skipped [{material, why}], cons per id).
    Unknown suitability stays allowed with a con ("not verified"); a poor rating or too little heat resistance
    drops it. A locked material is kept whatever (the user chose it), with its problems as cons."""
    mats = presets()["materials"]
    printer = base.printer
    temp = env.get("temp_c")
    touches = env.get("touches") or []
    ids = [lock] if lock else [m for m in (allowed or mats) if m in mats]
    keep, skipped, cons = [], [], {}
    for mid in ids:
        m = mats[mid]
        why, con = [], []
        if temp is not None and m.get("heat_limit_c") is not None and m["heat_limit_c"] < float(temp) + 10:
            why.append(f"softens near {m['heat_limit_c']:.0f} °C")
        if (m.get("abrasive") or m.get("fibre", "none") != "none") and not printer.get("hardened_nozzle", False):
            why.append("needs a hardened nozzle")
        for touch in touches:
            if touch == "outdoors":
                rating = m.get("uv_resistance")
            elif touch == "food":
                con.append("Food: needs a certified spool and a coating")
                continue
            else:
                rating = m.get(TOUCH_FIELD.get(touch, ""), None)
            if rating == "poor":
                why.append(f"poor with {TOUCH_WORDS.get(touch, touch)}")
            elif rating is None and touch in TOUCH_FIELD:
                con.append(f"Not verified for {TOUCH_WORDS[touch]}")
        if why and mid != lock:
            skipped.append({"material": mid, "why": "; ".join(why)})
            continue
        if why:
            con = [w.capitalize() for w in why] + con
        if m.get("warp_risk") == "high" and printer.get("enclosure") == "none":
            con.append("Warps on an open printer")
        keep.append(mid)
        cons[mid] = con
    return keep, skipped, cons


def layer_heights(goal: str, nozzle: float, lock: float | None, depth: str) -> list[float]:
    """Layer heights to try for a nozzle: the usual 0.5 × nozzle; Fast also thicker (0.6 / 0.7 ×, bonding drops
    above ~0.7 ×, strength.layer_bond_factor); Quality also thinner. Fast depth tries only the usual one."""
    if lock:
        return [float(lock)]
    factors = [0.5]
    if depth != "fast":
        factors += {"fast": [0.6, 0.7], "quality": [0.4]}.get(goal, [])
    return sorted({round(round(f * nozzle / 0.02) * 0.02, 2) for f in factors})


def _distinct(a: dict, b: dict) -> bool:
    if a["material"] != b["material"] or a["nozzle"] != b["nozzle"]:
        return True
    dt = abs(a["minutes"] - b["minutes"]) / max(a["minutes"], b["minutes"], 1e-6)
    dg = abs(a["total_g"] - b["total_g"]) / max(a["total_g"], b["total_g"], 1e-6)
    return dt >= DISTINCT_TIME or dg >= DISTINCT_GRAMS


def goal_cost(o: dict, goal: str, ref: dict) -> float:
    """Lower is better, among options that meet the target (§10.2 "then optimise for")."""
    t, g = o["minutes"] / max(ref["minutes"], 1e-6), o["total_g"] / max(ref["total_g"], 1e-6)
    ease = WARP_COST.get(o.get("warp_risk"), 0.0) - (OWNED_BONUS if o.get("owned") else 0.0)
    sup = (o["support_g"] or 0.0) / max(ref["total_g"], 1e-6)
    sf = o["safety"] if o["safety"] is not None else 0.0
    if goal == "strong":
        return -min(sf, 50.0) + 0.05 * t + 0.2 * ease
    if goal == "fast":
        return t + 0.05 * g + ease
    if goal == "light":
        return g + 0.05 * t + ease  # total grams include the support, which matters most here
    if goal == "quality":
        return o["ease"] + 0.5 * sup + 0.1 * t + ease
    return t + g + 0.3 * sup - 0.1 * min(sf / max(o["target"] or 1, 1e-6), 2.0) + ease  # balanced


def pick_options(cands: list[dict], goal: str, owned: list[str]) -> list[dict]:
    """2–4 options that really differ: best for the goal, least support, a material you have (or PLA), fastest.
    Never padded (§10.3 step 8)."""
    ok = [c for c in cands if c["meets_target"]]
    if not ok:
        return []
    ref = min(ok, key=lambda c: c["minutes"])
    ranked = sorted(ok, key=lambda c: goal_cost(c, goal, ref))
    picks = [dict(ranked[0], badge="Recommended", recommended=True)]

    def add(c, badge):
        if c is not None and len(picks) < MAX_OPTIONS and all(_distinct(c, p) for p in picks):
            picks.append(dict(c, badge=badge, recommended=False))

    known = [c for c in ok if c["support_g"] is not None]
    add(min(known, key=lambda c: (c["support_g"], goal_cost(c, goal, ref)), default=None), "Least support")
    shelf = [c for c in ok if c["material"] in owned] or [c for c in ok if c["material"] == "pla"]
    add(min(shelf, key=lambda c: goal_cost(c, goal, ref), default=None), "Material you have" if owned else "PLA only")
    add(min(ok, key=lambda c: c["minutes"]), "Fastest")
    if goal != "quality":
        add(min(ok, key=lambda c: c["total_g"]), "Least plastic")
        add(max(ok, key=lambda c: c["safety"] or 0.0), "Strongest")
    return picks


def pros_cons(opt: dict, best: dict, cons_by_material: dict[str, list[str]]) -> None:
    """At most 3 pros and 3 cons from checked numbers, relative to the recommended option (§11)."""
    pros, cons = [], []
    if opt is not best:
        dm = opt["minutes"] - best["minutes"]
        dg = opt["total_g"] - best["total_g"]
        if dm <= -1:
            pros.append(f"{-dm:.0f} min faster")
        elif dm >= 1:
            cons.append(f"{dm:.0f} min slower")
        if dg <= -1:
            pros.append(f"{-dg:.0f} g less plastic")
        elif dg >= 1:
            cons.append(f"{dg:.0f} g more plastic")
        if opt["support_g"] is not None and best["support_g"] is not None and opt["support_g"] < best["support_g"] - 0.5:
            pros.append(f"{best['support_g'] - opt['support_g']:.0f} g less support")
    else:
        if opt["support_g"] is not None and opt["support_g"] < 0.5:
            pros.append("No support")
        if opt["safety"] is not None and opt["target"]:
            pros.append(f"Safety ×{opt['safety']:.1f}")
    if float(opt["nozzle"]) != 0.4:
        cons.append(f"Needs a {opt['nozzle']} nozzle")
    cons += cons_by_material.get(opt["material"], [])
    opt["pros"], opt["cons"] = pros[:3], cons[:3]


def ways_out(base_change: str, material_locked: bool, best_sf: float | None, target: float) -> list[dict]:
    """One-click ways out when nothing reaches the target (§10.2)."""
    out = []
    if material_locked:
        out.append({"id": "allow_materials", "label": "Allow other materials"})
    if base_change == "slicer":
        out.append({"id": "allow_design", "label": "Allow design changes"})
    if best_sf is not None:
        out.append({"id": "use_anyway", "label": f"Use ×{best_sf:.1f} anyway", "warning": f"Below the ×{target:g} target"})
    return out


def _shell_estimate(mesh, sh, p: Profile) -> tuple[float, float]:
    """Grams and minutes without a strength model (Quality, no loads): walls over the whole surface, infill inside."""
    from .checks import shell_infill_minutes

    vol = abs(mesh.volume())
    area = float(mesh.normals_and_areas()[1].sum())
    shell = min(vol, area * max(sh.walls * p.line_width, (sh.top + sh.bottom) / 2 * p.layer_height))
    infill = (vol - shell) * sh.infill
    return (shell + infill) / 1000 * p.material["density_g_cm3"], shell_infill_minutes(shell, infill, p)


def optimize(mesh, *, goal: str = "balanced", loads: list[dict] | None = None, depth: str = "extended",
             base: Profile, owned: list[str] | None = None, materials: list[str] | None = None,
             nozzles: list[str] | None = None, environment: dict | None = None, constraints: dict | None = None,
             change: str = "slicer", load_case=None, climb=None, grams_minutes=None, evaluate=None,
             orientation_settings=None) -> dict:
    """See module docstring. ``load_case``/``climb``/``grams_minutes``/``evaluate``/``orientation_settings`` are
    the session-bound helpers from api.py (api.optimize passes them), so the strength model is shared and cached."""
    from .checks import orient
    from .orientation import candidate_directions, evaluate as evaluate_pose, find_orientations
    from .placement import print_rotation
    from .refine import support_stats
    from .strength import Shells

    if goal not in GOALS:
        raise ValueError(f"Unknown goal: {goal} (use one of {', '.join(GOALS)})")
    if depth not in DEPTHS:
        raise ValueError(f"Unknown depth: {depth} (use one of {', '.join(DEPTHS)})")
    if change not in CHANGES:
        raise ValueError(f"Unknown change: {change} (use one of {', '.join(CHANGES)})")
    lock = dict(constraints or {})
    env = dict(environment or {})
    owned = list(owned or [])
    strength_on = goal != "quality" and bool(loads)
    target = target_for(goal, base.constant_load) if strength_on else None
    skipped: list[dict] = []
    if depth == "complete" or change in ("design", "both"):
        skipped.append({"what": "design additions", "why": "Testing reinforcements at weak spots comes with the Complete depth (T-523)."})
    if goal != "quality" and not loads:
        skipped.append({"what": "strength", "why": "No loads: strength wasn't checked."})

    mats, mat_skipped, mat_cons = allowed_materials(base, env, lock.get("material"), materials)
    if not mats:
        return {"feasible": False, "target": target, "options": [], "skipped": skipped, "materials_skipped": mat_skipped,
                "ways_out": [{"id": "allow_materials", "label": "Allow other materials"}], "explanation": "No material fits where it's used."}
    known_nozzles = sorted(presets()["nozzles"], key=float)
    ticked = [n for n in (nozzles or known_nozzles) if n in presets()["nozzles"]]
    noz = [lock["nozzle"]] if lock.get("nozzle") else (["0.4"] if "0.4" in ticked else ticked[:1])
    bigger = [] if lock.get("nozzle") or depth == "fast" else [n for n in ticked if float(n) > 0.4]

    def prof(material: str, nozzle: str, lh: float | None = None) -> Profile:
        # goal "balanced" (layer factor 1): the layer height is given explicitly, so bonding and time agree on it.
        return replace(base, material_id=material, nozzle=nozzle, goal="balanced", layer_height_mm=lh)

    # ---------- 1. Orientations: separate shortlists so a strong pose isn't filtered out for printability. ----------
    progress.report(0.0, "Finding orientations")
    p_ref = prof(mats[0], noz[0])
    settings = orientation_settings(p_ref)
    n_poses = DEPTH_POSES[depth]
    found = find_orientations(mesh, settings, top=2 * n_poses)
    as_loaded = evaluate_pose(mesh, [0, 0, 1], settings)
    pool = found + [as_loaded]
    cases = load_case(loads) if strength_on else None
    if strength_on:
        from .strength import cases_safety

        dirs = np.vstack([candidate_directions(mesh, settings), [r.up for r in pool]])
        hints = cases_safety(cases, dirs, p_ref, _plan_shells(p_ref))
        strongest = [dirs[i] for i in np.argsort(-hints)[:2]]
        pool += [evaluate_pose(mesh, u, settings) for u in strongest]
        pool_hints = cases_safety(cases, np.array([r.up for r in pool]), p_ref, _plan_shells(p_ref))
    else:
        pool_hints = [None] * len(pool)
    poses = _shortlist(pool, pool_hints, n_poses, strength_on, lock.get("up"))
    progress.report(0.15, "Finding orientations")

    # ---------- 2. Support grams per pose, sliced, cached per (pose, overhang angle, layer height). ----------
    support_cache: dict = {}
    bed, moving = base.printer["bed"], bool(base.printer.get("moving_bed", False))

    def support_g(i: int, p: Profile) -> float | None:
        key = (i, p.material["max_overhang_deg"], p.layer_height, p.line_width)
        if key not in support_cache:
            try:
                rot, _ = print_rotation(mesh, poses[i].up, bed, moving)
                st = support_stats(orient(mesh, rot), p, max_layers=40)
                support_cache[key] = st["support_volume"] * SUPPORT_FILL / 1000  # cm³; grams per material below
            except Exception:  # noqa: BLE001 - an unknown support estimate is shown as unknown, never as zero
                support_cache[key] = None
        v = support_cache[key]
        return None if v is None else v * p.material["density_g_cm3"]

    # ---------- 3–6. Recipe search per pose, verified; materials by arithmetic on the verified solve. ----------
    cands: list[dict] = []
    recipe_fixed = change == "design"

    def option(i: int, p: Profile, sh, sf: float | None, fi: tuple[float, float] | None, model_gm) -> None:
        g_part, minutes = model_gm(sh, p)
        sup = support_g(i, p)
        from .api import _grams_minutes

        total = g_part + (sup or 0.0)
        minutes += _grams_minutes(sup, p) if sup else 0.0
        cands.append({
            "up": [round(float(x), 6) for x in poses[i].up], "orientation": poses[i].label, "shortlists": sorted(poses[i].tags),
            "material": p.material_id, "material_name": p.material.get("name", p.material_id), "nozzle": p.nozzle,
            "layer_height": round(p.layer_height, 2), "walls": sh.walls, "top": sh.top, "bottom": sh.bottom, "infill": round(sh.infill, 2),
            "part_g": round(g_part, 1), "support_g": None if sup is None else round(sup, 1), "total_g": round(total, 1),
            "minutes": round(minutes, 1), "safety": None if sf is None else round(float(sf), 2), "target": target,
            "meets_target": target is None or (sf is not None and sf >= target), "ease": poses[i].ease,
            "owned": p.material_id in owned, "warp_risk": p.material.get("warp_risk"), "_fi": fi,
        })

    def no_model_gm(sh, p):
        return _shell_estimate(mesh, sh, p)

    def gm_for(i: int):
        if not strength_on:
            return no_model_gm
        return lambda sh, q, u=poses[i].up: grams_minutes(cases[0].model, u, sh, q, mesh)

    def scan(i: int, nozzle: str) -> None:
        """One solve per layer height with the goal's usual recipe (or the fixed one); every material by arithmetic."""
        for lh in layer_heights(goal, float(nozzle), lock.get("layer_height"), depth):
            p0 = prof(mats[0], nozzle, lh)
            sh = _locked_shells(_plan_shells(p0, goal), lock)
            fi = None
            if strength_on:
                r = evaluate(cases, poses[i].up, p0, sh)
                fi = (r["fi_layers_max"], r["fi_material_max"])
            for mid in mats:
                q = prof(mid, nozzle, lh)
                option(i, q, sh, _material_safety(fi, p0, q) if fi else None, fi, gm_for(i))

    own_search = ([m for m in mats[1:] if m in owned] + [m for m in ("pla", "petg") if m in mats[1:] and m not in owned])[:OWN_SEARCH_MATERIALS]

    def refine(i: int, nozzle: str, own_climbs: bool = True) -> None:
        """The greedy wall / top / infill search (api._climb, full anisotropic solves) for one pose: the lightest
        recipe that reaches the target, for the first material; other materials reuse its verified solve when the
        same recipe is enough for them, else (Extended) get their own search."""
        for lh in layer_heights(goal, float(nozzle), lock.get("layer_height"), depth):
            p0 = prof(mats[0], nozzle, lh)
            ref = climb(cases, poses[i].up, p0, target, mesh)
            sh = _locked_shells(Shells(walls=ref["walls"], infill=ref["infill"], top=ref["top"], bottom=ref["bottom"]), lock)
            r = evaluate(cases, poses[i].up, p0, sh)
            fi = (r["fi_layers_max"], r["fi_material_max"])
            for mid in mats:
                q = prof(mid, nozzle, lh)
                sf = _material_safety(fi, p0, q)
                if sf >= target or depth == "fast" or mid == mats[0] or not own_climbs or mid not in own_search:
                    option(i, q, sh, sf, fi, gm_for(i))
                    continue
                own = climb(cases, poses[i].up, q, target, mesh)
                sh2 = _locked_shells(Shells(walls=own["walls"], infill=own["infill"], top=own["top"], bottom=own["bottom"]), lock)
                r2 = evaluate(cases, poses[i].up, q, sh2)
                option(i, q, sh2, r2["safety_factor"], (r2["fi_layers_max"], r2["fi_material_max"]), gm_for(i))

    def trial(i: int, nozzle: str, rows: list[dict]) -> None:
        """A bigger nozzle on the best pose: the goal's usual recipe, plus each 0.4 option's recipe at the same
        wall and skin thickness in mm (one solve each, materials by arithmetic) — enough to see whether it gains."""
        scan(i, nozzle)
        if not strength_on or recipe_fixed:
            return
        lw0 = prof(mats[0], noz[0]).line_width
        seen = set()
        for row in rows:
            for lh in layer_heights(goal, float(nozzle), lock.get("layer_height"), depth):
                p0 = prof(mats[0], nozzle, lh)
                sh = _locked_shells(Shells(walls=max(2, round(row["walls"] * lw0 / p0.line_width)), infill=row["infill"],
                                           top=max(3, round(row["top"] * row["layer_height"] / lh)),
                                           bottom=max(2, round(row["bottom"] * row["layer_height"] / lh))), lock)
                if (sh, lh) in seen:
                    continue
                seen.add((sh, lh))
                r = evaluate(cases, poses[i].up, p0, sh)
                fi = (r["fi_layers_max"], r["fi_material_max"])
                for mid in mats:
                    q = prof(mid, nozzle, lh)
                    option(i, q, sh, _material_safety(fi, p0, q), fi, gm_for(i))

    # Phase A: every pose once (cheap). Phase B: the recipe search only on the most promising poses.
    for i in range(len(poses)):
        progress.report(0.15 + 0.35 * i / max(len(poses), 1), "Checking strength" if strength_on else "Measuring support")
        scan(i, noz[0])
    if strength_on and not recipe_fixed:
        ref_c = min(cands, key=lambda c: c["minutes"])
        ranked = sorted(range(len(poses)), key=lambda i: min(
            (goal_cost(c, goal, ref_c) if c["meets_target"] else 1e6 - (c["safety"] or 0.0)
             for c in cands if c["up"] == [round(float(x), 6) for x in poses[i].up]), default=1e9))
        top = ranked[:REFINE_POSES[depth]]
        for k, i in enumerate(top):
            progress.report(0.5 + 0.3 * k / len(top), "Trying walls, top layers and infill")
            refine(i, noz[0], own_climbs=k == 0)  # other materials' own search only on the best pose
    step = len(poses)

    # ---------- 4. Bigger nozzles: only if the part still prints with them and the gain is significant. ----------
    nozzle_notes: list[dict] = []
    if bigger and cands:
        best_i = _pose_index(poses, min((c for c in cands if c["meets_target"]), key=lambda c: c["minutes"], default=cands[0])["up"])
        base_rows = [c for c in cands if c["up"] == [round(float(x), 6) for x in poses[best_i].up] and c["nozzle"] == noz[0]]
        for n in bigger:
            progress.report(0.8 + 0.1 * (step - len(poses)) / max(len(bigger), 1), "Trying nozzles")
            step += 1
            lost = _nozzle_blockers(mesh, poses[best_i].up, prof(mats[0], n), bed, moving)
            if lost:
                nozzle_notes.append({"nozzle": n, "why": f"{n} mm skipped: {lost}"})
                continue
            before = len(cands)
            trial(best_i, n, base_rows)
            new = cands[before:]
            gain = any(_significant(c, base_rows) for c in new)
            if not gain:
                del cands[before:]
                nozzle_notes.append({"nozzle": n, "why": f"{n} mm skipped: not much faster or stronger than 0.4 on this part"})

    # ---------- 8. Pick 2–4 distinct options; honest result when nothing reaches the target. ----------
    progress.report(0.9, "Picking options")
    for c in cands:
        c.pop("_fi", None)
    picks = pick_options(cands, goal, owned)
    feasible = bool(picks)
    out = {"goal": goal, "depth": depth, "target": target, "feasible": feasible, "skipped": skipped,
           "materials_skipped": mat_skipped, "nozzles_skipped": nozzle_notes, "candidates_checked": len(cands)}
    if not feasible:
        best = max(cands, key=lambda c: c["safety"] or 0.0) if cands else None
        if best is not None:
            picks = [dict(best, badge="Best reachable", recommended=True, below_target=True)]
        out["ways_out"] = ways_out(change, bool(lock.get("material")) or bool(materials), best and best["safety"], target)
        out["explanation"] = (f"None of the options we checked reached ×{target:g}. Best: ×{best['safety']:.1f}" if best and best["safety"]
                              else "Nothing could be checked.")
    for o in picks:
        pros_cons(o, picks[0], mat_cons)
        o["settings"] = _slicer_settings(o)
    out["options"] = picks
    progress.report(1.0, "Picking options")
    return out


def _plan_shells(p: Profile, goal: str = "balanced"):
    """The goal's usual walls / infill / top / bottom as Shells (the starting recipe; recipe fixed in design-only)."""
    from .recommend import goal_plan
    from .strength import Shells

    profile_goal = {"strong": "strength", "fast": "speed", "light": "material"}.get(goal, "balanced")
    plan = goal_plan(replace(p, goal=profile_goal))
    sh = Shells(walls=int(plan["walls"]), infill=float(plan["infill"]) / 100, top=int(plan["top"]), bottom=int(plan["bottom"]))
    return replace(sh, walls=2, infill=0.10) if goal == "quality" else sh


def _locked_shells(sh, lock: dict):
    """The user's pinned values ("Your choices") are hard limits."""
    return replace(sh, **{k: (float(lock[k]) if k == "infill" else int(lock[k])) for k in ("walls", "top", "bottom", "infill") if lock.get(k) is not None})


def _material_safety(fi: tuple[float, float], p_ref: Profile, p: Profile) -> float:
    """Safety for another material from the reference solve. Stress doesn't depend on a uniform stiffness change
    (the layer-to-in-plane stiffness ratio is the same for every material), so only the strengths scale."""
    from .strength import material_for

    a, b = material_for(p_ref), material_for(p)
    worst = max(fi[0] * a.strength_z / b.strength_z, fi[1] * a.strength_xy / b.strength_xy)
    return float(1 / worst) if worst > 0 else float("inf")


def _shortlist(pool: list, hints, n: int, strength_on: bool, locked_up) -> list[Pose]:
    """Round-robin over the shortlists (easiest, least support, fastest = lowest, strongest), distinct directions."""
    if locked_up is not None:
        u = np.asarray(locked_up, float) / np.linalg.norm(locked_up)
        r = min(pool, key=lambda r: -float(np.dot(r.up, u)))
        return [Pose(u, pose_label(u), r.score, r.support_volume, r.height, tags={"yours"})]
    idx = list(range(len(pool)))
    lists = {
        "easiest": sorted(idx, key=lambda i: pool[i].score),
        "least_support": sorted(idx, key=lambda i: (pool[i].support_volume, pool[i].score)),
        "fastest": sorted(idx, key=lambda i: (pool[i].height + pool[i].support_volume ** (1 / 3), pool[i].score)),
    }
    if strength_on:
        lists["strongest"] = sorted(idx, key=lambda i: -(hints[i] or 0.0))
    picked: list[Pose] = []
    cursor = {k: 0 for k in lists}
    while len(picked) < n and any(cursor[k] < len(v) for k, v in lists.items()):
        for name, order in lists.items():
            while cursor[name] < len(order):
                r = pool[order[cursor[name]]]
                cursor[name] += 1
                same = next((p for p in picked if float(np.dot(p.up, r.up)) > 0.999), None)
                if same is not None:
                    same.tags.add(name)
                    continue
                picked.append(Pose(np.asarray(r.up, float), pose_label(r.up), r.score, r.support_volume, r.height,
                                   None if hints[order[cursor[name] - 1]] is None else float(hints[order[cursor[name] - 1]]), {name}))
                break
            if len(picked) >= n:
                break
    return picked


def _pose_index(poses: list[Pose], up) -> int:
    u = np.asarray(up, float)
    return int(np.argmax([float(np.dot(p.up, u)) for p in poses]))


def _significant(c: dict, base_rows: list[dict]) -> bool:
    same = [b for b in base_rows if b["material"] == c["material"]]
    if not same:
        return False
    b = min(same, key=lambda r: r["minutes"])
    faster = c["minutes"] <= (1 - NOZZLE_GAIN) * b["minutes"]
    stronger = c["safety"] is not None and b["safety"] is not None and c["safety"] >= (1 + NOZZLE_GAIN) * b["safety"]
    return c["meets_target"] and (faster or stronger)


def _nozzle_blockers(mesh, up, p: Profile, bed, moving: bool) -> str:
    """Why this nozzle can't print the part (new nozzle problems or lost detail), or "" (decision 7)."""
    from .checks import NOZZLE_RULES, lost_detail, orient, run_checks
    from .placement import print_rotation
    from .slicer import slice_mesh

    rot, _ = print_rotation(mesh, up, bed, moving)
    oriented = orient(mesh, rot)
    r = run_checks(oriented, p, max_layers=80)
    issues = sorted({f["title"] for f in r["findings"] if f["rule"] in NOZZLE_RULES})
    ref = run_checks(oriented, replace(p, nozzle="0.4"), max_layers=80)
    new = [t for t in issues if t not in {f["title"] for f in ref["findings"] if f["rule"] in NOZZLE_RULES}]
    if new:
        return new[0][0].lower() + new[0][1:]
    lost = lost_detail(slice_mesh(oriented, 0.2, 80), float(p.nozzle))["lost_mm3"]
    return f"about {lost:.0f} mm³ of small detail would be lost" if lost > 1.0 else ""


def _slicer_settings(o: dict) -> dict:
    """The option as OrcaSlicer keys (strings, as the 3MF export writes them)."""
    return {"wall_loops": str(o["walls"]), "top_shell_layers": str(o["top"]), "bottom_shell_layers": str(o["bottom"]),
            "sparse_infill_density": f"{o['infill'] * 100:.0f}%", "layer_height": f"{o['layer_height']:.2f}",
            "nozzle_diameter": o["nozzle"]}
