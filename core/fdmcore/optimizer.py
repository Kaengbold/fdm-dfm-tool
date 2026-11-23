"""The optimizer behind the new UI's Analyse and Options steps (T-522 / T-523, plan
research/redesign-2026-11/PLAN-COMBINED.md §10.1, §10.2, §10.3, §11, §14, §16).

For the loaded part it searches orientation × material × nozzle × layer height × walls/top/infill,
plus Complete-depth local reinforcements (modifier zones, hidden gaps, inside-corner fillets and
local thickening tested in voxel space — no mesh booleans in Python), and returns 2–4 options that
really differ, each verified with the full direction-dependent strength check at its own pose and
settings. Built from the engine's existing pieces (orientation scoring, sliced support estimate,
the greedy shell search ``api._climb``), so a number here means the same as everywhere else.

Rules from the owner (§16):
- Safety targets are floors: Strong ×5, Balanced ×3, Fast ×2, Light ×1.5; a load that stays on raises it to ×3.
  Nothing below the floor is offered as meeting it; if nothing reaches it, the best reachable comes back marked
  ``below_target`` with the ways out (decision 2: usable after a warning).
- Quality and "no loads" never run a strength solve (§14).
- Nozzles only from the user's ticked list; one bigger than 0.4 only if the part still prints with it and the gain
  is significant (decision 7, no size cut-off).
- "The design" only (decision 4): the recipe stays at the baseline; orientation and design additions (hidden gaps,
  fillets, local thickening) are searched at every depth (cheap candidates at Fast/Extended, fuller at Complete).
- Complete depth (§10.3 point 7): on the best 2 poses, tries modifier zones (slicer-only / both) and hidden gaps,
  fillets and local thickening (design / both) in voxel space; only winning options get edit meshes (built in
  the browser with manifold-3d), and every reinforced option is re-checked before claiming an improvement.

Pyodide-safe: numpy + scipy (lazy) + shapely (lazy, only for winning edit meshes) + pyyaml + stdlib.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace

import numpy as np

from . import progress
from .profile import Profile, load_data, presets

GOAL_FLOOR = {"strong": 5.0, "balanced": 3.0, "fast": 2.0, "light": 1.5, "quality": None}
SUSTAINED_FLOOR = 3.0  # a load that never lets go creeps: at least ×3 whatever the goal (service.CONSTANT_LOAD_SAFETY)
GOALS = tuple(GOAL_FLOOR)
DEPTHS = ("fast", "extended", "complete")
DEPTH_POSES = {"fast": 3, "extended": 6, "complete": 6}
REFINE_POSES = {"fast": 1, "extended": 3, "complete": 3}  # poses that get the full recipe search
REINF_POSES = {"fast": 1, "extended": 2, "complete": 2}  # poses that get reinforcement testing
NOZZLE_GAIN = 0.15  # a bigger nozzle must save ≥ 15 % time or add ≥ 15 % safety over 0.4 to be offered (§10.3)
DISTINCT_TIME, DISTINCT_GRAMS = 0.10, 0.15  # options must differ this much, or in material / nozzle / reinforcement
MAX_OPTIONS = 4
WARP_COST = {"high": 0.3, "medium": 0.1}  # harder-to-print materials need a real gain to be recommended
OWNED_BONUS = 0.15
OWN_SEARCH_MATERIALS = 2  # Extended/Complete: materials (owned first, else PLA / PETG) that get their own recipe search
SUPPORT_FILL = 0.25  # as api.SUPPORT_FILL: support is printed sparse
CHANGES = ("slicer", "design", "both")
# What the "What will it touch?" chips check in the material data (T-520 fields; outdoors = UV).
TOUCH_FIELD = {"water": "water", "abrasion": "abrasion", "oil_fuel": "oil_fuel", "solvent": "solvent", "skin": "skin"}
TOUCH_WORDS = {"water": "water", "abrasion": "scraping", "oil_fuel": "oil and fuel", "solvent": "cleaners", "skin": "skin",
               "outdoors": "sun and rain", "food": "food"}

# Complete / design-only reinforcement sizing and gates.
REINF_MIN_SAFETY_GAIN = 0.08  # reinforced option must be ≥ 8 % stronger to claim a strength win
REINF_MIN_GRAM_SAVE = 0.05  # or ≥ 5 % lighter (and ≥ 0.3 g) while meeting target
REINF_MIN_GRAM_SAVE_ABS = 0.3
REINF_MAX_EXTRA_PLASTIC = 0.35  # cap extra plastic for non-Strong goals when already feasible
VOID_MM = 0.6
VOID_COVER_MM = 1.0
FILLET_SIZES = {"cheap": (2.5,), "full": (2.5, 4.0)}
THICKEN_SIZES = {"cheap": (1.5,), "full": (1.5, 3.0)}
GAP_PIN_COUNTS = {"cheap": (3,), "full": (3, 6)}


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
    if a.get("reinforcement_kind") != b.get("reinforcement_kind"):
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
    """2–4 options that really differ: best for the goal, least support, a material you have (or PLA),
    fastest, or Reinforced (Complete / design-only). Never padded (§10.3 step 8)."""
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
    reinf = [c for c in ok if c.get("reinforced")]
    if reinf:
        best_reinf = min(reinf, key=lambda c: goal_cost(c, goal, ref))
        add(best_reinf, "Reinforced")
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
        if (opt.get("safety") is not None and best.get("safety") is not None
                and opt["safety"] >= best["safety"] * 1.1 and opt.get("reinforced")):
            pros.append(f"Safety ×{opt['safety']:.1f}")
    else:
        if opt["support_g"] is not None and opt["support_g"] < 0.5:
            pros.append("No support")
        if opt["safety"] is not None and opt["target"]:
            pros.append(f"Safety ×{opt['safety']:.1f}")
    if opt.get("reinforced") and opt.get("reinforcements"):
        pros.append(opt["reinforcements"][0]["label"])
        if opt.get("edits"):
            cons.append("Changes the part geometry")
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


def _format_duration(seconds: float) -> str:
    s = max(1, int(round(seconds)))
    if s < 60:
        return f"{s} s"
    m, rem = divmod(s, 60)
    return f"{m} min {rem} s" if rem >= 10 else f"{m} min"


def estimate_runtime(mesh, *, depth: str = "extended", loads: list[dict] | None = None, base: Profile | None = None,
                     goal: str = "balanced", owned: list[str] | None = None, materials: list[str] | None = None,
                     nozzles: list[str] | None = None, environment: dict | None = None,
                     constraints: dict | None = None, change: str = "slicer", target_voxels: int = 12_000) -> dict:
    """Calibrated runtime estimate for ``optimize`` from triangle count, part size, voxel count and
    candidate count (plan §10.1). Pure numpy + yaml — never triggers scipy or FEA solves.

    Returns seconds for this machine (``seconds``) and the browser estimate (``browser_seconds``,
    ~3× slower in Pyodide)."""
    if goal not in GOALS:
        raise ValueError(f"Unknown goal: {goal} (use one of {', '.join(GOALS)})")
    if depth not in DEPTHS:
        raise ValueError(f"Unknown depth: {depth} (use one of {', '.join(DEPTHS)})")
    if change not in CHANGES:
        raise ValueError(f"Unknown change: {change} (use one of {', '.join(CHANGES)})")
    base = base or Profile()
    lock = dict(constraints or {})
    env = dict(environment or {})
    env.setdefault("temp_c", base.use_temp_c)
    cal = load_data("optimizer_timing")

    n_tris = int(len(mesh.triangles))
    lo, hi = mesh.bounds()
    size = hi - lo
    max_dim = float(size.max())
    vol = max(abs(float(mesh.volume())), 1e-3)

    strength_on = goal != "quality" and bool(loads)
    if strength_on:
        h = max((vol / max(int(target_voxels), 100)) ** (1 / 3), max_dim / 200.0)
        est_voxels = int(min(int(target_voxels), max(100, round(vol / max(h**3, 1e-9)))))
        forces = [l for l in loads if l.get("kind") != "fixed"]
        n_cases = len({int(l.get("case", 1)) for l in forces}) or 1
    else:
        est_voxels = 0
        n_cases = 0

    recipe_fixed = change == "design"
    mats, _, _ = allowed_materials(base, env, lock.get("material"), materials)
    known_nozzles = sorted(presets()["nozzles"], key=float)
    ticked = [n for n in (nozzles or known_nozzles) if n in presets()["nozzles"]]
    noz = [lock["nozzle"]] if lock.get("nozzle") else (["0.4"] if "0.4" in ticked else ticked[:1])
    bigger = [] if lock.get("nozzle") or depth == "fast" or recipe_fixed else [n for n in ticked if float(n) > 0.4]
    n_lh = len(layer_heights(goal, float(noz[0]), lock.get("layer_height"), "fast" if recipe_fixed else depth)) if noz else 1

    n_poses = 1 if lock.get("up") is not None else DEPTH_POSES[depth]
    n_refine = 0 if (not strength_on or recipe_fixed) else min(n_poses, REFINE_POSES[depth])
    n_own = 0 if (not strength_on or recipe_fixed or depth == "fast" or len(mats) <= 1) else min(len(mats) - 1, OWN_SEARCH_MATERIALS)

    # Reinforcement solves & edit mesh count
    run_zones = strength_on and depth == "complete" and change in ("slicer", "both")
    run_design = strength_on and ((depth == "complete" and change in ("design", "both")) or change == "design")
    n_reinf_poses = min(n_poses, REINF_POSES[depth]) if (run_zones or run_design) else 0
    full_reinf = depth == "complete"

    est_solves = 0.0
    est_reinf_solves = 0.0
    if strength_on and mats:
        est_solves += cal["solves_orientation_hint"]
        est_solves += n_poses * n_lh * cal["solves_per_scan_pose"]
        est_solves += n_refine * n_lh * cal["solves_per_refine_pose"]
        est_solves += n_own * n_lh * cal["solves_per_own_material"]
        if bigger:
            est_solves += len(bigger) * n_lh * cal["solves_per_bigger_nozzle"]
        if run_zones:
            est_reinf_solves += n_reinf_poses * cal["solves_per_zone_pose"]
        if run_design:
            est_reinf_solves += n_reinf_poses * cal["solves_per_gap_pose_full" if full_reinf else "solves_per_gap_pose_cheap"]
            est_reinf_solves += n_reinf_poses * cal["solves_per_design_pose_full" if full_reinf else "solves_per_design_pose_cheap"]

    avg_dim = float(np.mean(size))
    size_fac = 0.35 + cal["size_scale"] * min(avg_dim / 50.0, 2.0)
    tk = (n_tris / 1000.0) ** cal["tri_exp"]
    tri_fac = (1.0 + cal["tri_scale"] * tk / (1.0 + cal["tri_sat"] * tk)) * size_fac
    native_s = float(cal["overhead_s"])
    if mats:
        native_s += cal["orient_per_tri_s"] * n_tris + cal["orient_per_pose_s"] * n_poses
        native_s += cal["support_per_pose_s"] * n_poses * tri_fac
        native_s += cal["nozzle_check_s"] * len(bigger) * tri_fac
        if strength_on:
            native_s += cal["voxelize_base_s"] + cal["voxelize_per_tri_s"] * n_tris + cal["voxelize_per_vox_s"] * est_voxels
            per_solve = cal["solve_per_kvox_s"] * (max(est_voxels, 100) / 1000.0) ** cal["voxel_exp"]
            native_s += per_solve * n_cases * (est_solves + est_reinf_solves)
            if run_design:
                native_s += cal["reinf_mesh_s"] * n_reinf_poses * tri_fac

    browser_factor = float(cal.get("browser_factor", 3.0))
    browser_s = native_s * browser_factor
    return {
        "depth": depth,
        "goal": goal,
        "change": change,
        "seconds": round(native_s, 1),
        "browser_seconds": round(browser_s, 1),
        "browser_factor": browser_factor,
        "summary": f"About {_format_duration(native_s)} here (about {_format_duration(browser_s)} in the browser)",
        "features": {
            "triangles": n_tris,
            "size_mm": [round(float(x), 1) for x in size],
            "voxels": est_voxels,
            "candidates": {
                "poses": int(n_poses),
                "refine_poses": int(n_refine),
                "materials": int(len(mats)),
                "nozzles": int(len(noz) + len(bigger)),
                "layer_heights": int(n_lh),
                "load_cases": int(n_cases),
                "solves_est": round(est_solves, 1),
                "reinforcement_solves_est": round(est_reinf_solves, 1),
            },
        },
    }


def optimize(mesh, *, goal: str = "balanced", loads: list[dict] | None = None, depth: str = "extended",
             base: Profile, owned: list[str] | None = None, materials: list[str] | None = None,
             nozzles: list[str] | None = None, environment: dict | None = None, constraints: dict | None = None,
             change: str = "slicer", load_case=None, climb=None, grams_minutes=None, evaluate=None,
             orientation_settings=None, zone_info=None, unit_shape=None, mark_faces=None, budget_s: float | None = None) -> dict:
    """See module docstring. ``load_case``/``climb``/``grams_minutes``/``evaluate``/``orientation_settings``/
    ``zone_info``/``unit_shape`` are the session-bound helpers from api.py (api.optimize passes them), so
    the strength model is shared and cached. ``mark_faces(results, settings)`` (api._pose_marks) adds the marked
    faces' penalties to the orientation scores (Quality: support on faces people see).

    ``budget_s``: a time budget. Once it's spent the search skips what's left (more poses, recipe searches, nozzles,
    reinforcements) and picks from what it checked: the result says ``ended_early`` (with the stage it stopped in) and,
    when nothing checked reaches the target, offers "Continue the search" first, never "impossible". While it runs,
    the best options so far go out as partial results (progress.partial, ``partial: True``) after each stage."""
    from .checks import orient
    from .orientation import candidate_directions, evaluate as evaluate_pose, find_orientations
    from .placement import print_rotation
    from .refine import support_stats
    from .strength import Shells, evaluate_cases

    if goal not in GOALS:
        raise ValueError(f"Unknown goal: {goal} (use one of {', '.join(GOALS)})")
    if depth not in DEPTHS:
        raise ValueError(f"Unknown depth: {depth} (use one of {', '.join(DEPTHS)})")
    if change not in CHANGES:
        raise ValueError(f"Unknown change: {change} (use one of {', '.join(CHANGES)})")
    started = time.monotonic()
    ended_at: list[str] = []  # the stage the time budget ran out in (at most one)

    def over(stage: str) -> bool:
        """The budget is spent: remember where, and skip the rest."""
        if not ended_at and budget_s is not None and time.monotonic() - started > budget_s:
            ended_at.append(stage)
        return bool(ended_at)

    lock = dict(constraints or {})
    env = dict(environment or {})
    owned = list(owned or [])
    strength_on = goal != "quality" and bool(loads)
    target = target_for(goal, base.constant_load) if strength_on else None
    skipped: list[dict] = []
    if goal != "quality" and not loads:
        skipped.append({"what": "strength", "why": "No loads: strength wasn't checked."})

    mats, mat_skipped, mat_cons = allowed_materials(base, env, lock.get("material"), materials)
    if not mats:
        return {"feasible": False, "target": target, "options": [], "skipped": skipped, "materials_skipped": mat_skipped,
                "ways_out": [{"id": "allow_materials", "label": "Allow other materials"}], "explanation": "No material fits where it's used."}
    recipe_fixed = change == "design"
    known_nozzles = sorted(presets()["nozzles"], key=float)
    ticked = [n for n in (nozzles or known_nozzles) if n in presets()["nozzles"]]
    noz = [lock["nozzle"]] if lock.get("nozzle") else (["0.4"] if "0.4" in ticked else ticked[:1])
    bigger = [] if lock.get("nozzle") or depth == "fast" or recipe_fixed else [n for n in ticked if float(n) > 0.4]

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
    if mark_faces:
        mark_faces(pool, settings)
    cases = load_case(loads) if strength_on else None
    if strength_on:
        from .strength import cases_safety

        cand_dirs = candidate_directions(mesh, settings)
        n_cand = len(cand_dirs)
        dirs = np.vstack([cand_dirs, [r.up for r in pool]])
        hints = cases_safety(cases, dirs, p_ref, _plan_shells(p_ref))
        top2 = np.argsort(-hints)[:2]
        pool += [evaluate_pose(mesh, dirs[j], settings) for j in top2]
        pool_hints = np.concatenate([hints[n_cand:], hints[top2]])
    else:
        pool_hints = [None] * len(pool)
    poses = _shortlist(pool, pool_hints, n_poses, strength_on, lock.get("up"))
    progress.report(0.15, "Finding orientations")

    # ---------- 2. Support grams per pose, sliced, cached per (pose, overhang angle, effective slice dz, line width). ----------
    from .slicer import slice_mesh

    support_cache: dict = {}
    slice_cache: dict = {}
    oriented_pose: dict[int, tuple] = {}
    bed, moving = base.printer["bed"], bool(base.printer.get("moving_bed", False))

    def _oriented(i: int):
        if i not in oriented_pose:
            rot, _ = print_rotation(mesh, poses[i].up, bed, moving)
            om = orient(mesh, rot)
            h_mm = float(om.triangles[..., 2].max()) if len(om.triangles) else 0.0
            oriented_pose[i] = (om, h_mm)
        return oriented_pose[i]

    def support_g(i: int, p: Profile) -> float | None:
        om, h_mm = _oriented(i)
        n_lay = max(1, min(int(np.ceil(h_mm / max(p.layer_height, 1e-6))), 40)) if h_mm > 0 else 1
        eff_dz = round(h_mm / n_lay, 3) if h_mm > 0 else round(p.layer_height, 3)
        key = (i, p.material["max_overhang_deg"], eff_dz, round(p.line_width, 3))
        if key not in support_cache:
            try:
                skey = (i, eff_dz)
                if skey not in slice_cache:
                    slice_cache[skey] = slice_mesh(om, p.layer_height, 40)
                st = support_stats(om, p, max_layers=40, layers=slice_cache[skey])
                support_cache[key] = st["support_volume"] * SUPPORT_FILL / 1000  # cm³; grams per material below
            except Exception:  # noqa: BLE001 - an unknown support estimate is shown as unknown, never as zero
                support_cache[key] = None
        v = support_cache[key]
        return None if v is None else v * p.material["density_g_cm3"]

    # ---------- Shared material-independent FEA cache & preconditioner cache ----------
    # Because E_z/E_xy and Poisson ratios are shared across materials (strength.material_for), the stress field
    # for a given (pose, nozzle, layer_height, shells) is identical across all materials; only strengths scale.
    precond_cache: dict = {}
    solve_cache: dict = {}  # (pose_idx, nozzle, round(lh, 3), sh) -> (result_dict, p_solved, exact_bool)
    pose_u: dict[int, list] = {}  # latest displacement per pose for warm-starting nearby solves

    def cached_solve(i: int, p: Profile, sh, rtol: float = 1e-4, x0=None) -> tuple[dict, Profile]:
        key = (i, p.nozzle, round(p.layer_height, 3), sh)
        exact = rtol <= 1e-4
        hit = solve_cache.get(key)
        if hit is not None and (hit[2] or not exact):
            return hit[0], hit[1]
        start = hit[0].get("_u_all") if hit is not None else (x0 if x0 is not None else pose_u.get(i))
        # Use evaluate_cases directly with warm start and preconditioner reuse; keep material p0 as reference.
        p0 = prof(mats[0], p.nozzle, p.layer_height)
        r = evaluate_cases(cases, poses[i].up, p0, sh, x0=start, rtol=rtol, precond_cache=precond_cache)
        solve_cache[key] = (r, p0, exact)
        if r.get("_u_all") is not None:
            pose_u[i] = r["_u_all"]
        return r, p0

    def climb_eval(i: int, q: Profile):
        def _ev(sh, rtol: float = 1e-3, x0=None):
            r, p_ref_s = cached_solve(i, q, sh, rtol=rtol, x0=x0)
            sf = _material_safety(r["case_fi"], p_ref_s, q)
            return sf, r.get("_u_all")
        return _ev

    # ---------- 3–6. Recipe search per pose, verified; materials by arithmetic on the verified solve. ----------
    cands: list[dict] = []

    def option(i: int, p: Profile, sh, sf: float | None, model_gm, *, reinforced: bool = False,
               reinforcement_kind: str | None = None, reinforcements: list[dict] | None = None,
               zones: list[dict] | None = None, edits: list[dict] | None = None) -> dict:
        g_part, minutes = model_gm(sh, p)
        sup = support_g(i, p)
        from .api import _grams_minutes

        total = g_part + (sup or 0.0)
        minutes += _grams_minutes(sup, p) if sup else 0.0
        row = {
            "up": [round(float(x), 6) for x in poses[i].up], "orientation": poses[i].label, "shortlists": sorted(poses[i].tags),
            "material": p.material_id, "material_name": p.material.get("name", p.material_id), "nozzle": p.nozzle,
            "layer_height": round(p.layer_height, 2), "walls": sh.walls, "top": sh.top, "bottom": sh.bottom, "infill": round(sh.infill, 2),
            "part_g": round(g_part, 1), "support_g": None if sup is None else round(sup, 1), "total_g": round(total, 1),
            "minutes": round(minutes, 1), "safety": None if sf is None else round(float(sf), 2), "target": target,
            "meets_target": target is None or (sf is not None and sf >= target), "ease": poses[i].ease,
            "owned": p.material_id in owned, "warp_risk": p.material.get("warp_risk"),
            "reinforced": reinforced, "reinforcement_kind": reinforcement_kind,
            "reinforcements": list(reinforcements or []), "zones": list(zones or []), "edits": list(edits or []),
        }
        cands.append(row)
        return row

    def no_model_gm(sh, p):
        return _shell_estimate(mesh, sh, p)

    def gm_for(i: int, model=None, volume_mm3: float | None = None):
        if not strength_on:
            return no_model_gm
        mdl = cases[0].model if model is None else model
        if volume_mm3 is None:
            return lambda sh, q, u=poses[i].up, m=mdl: grams_minutes(m, u, sh, q, mesh)
        return lambda sh, q, u=poses[i].up, m=mdl, v=volume_mm3: grams_minutes(m, u, sh, q, mesh, volume_mm3=v)

    def scan(i: int, nozzle: str) -> None:
        """One solve per layer height with the goal's usual recipe (or the fixed one); every material by arithmetic."""
        for lh in layer_heights(goal, float(nozzle), lock.get("layer_height"), "fast" if recipe_fixed else depth):
            p0 = prof(mats[0], nozzle, lh)
            sh = _locked_shells(_plan_shells(p0, goal), lock)
            case_fi = None
            p_ref_s = p0
            if strength_on:
                r, p_ref_s = cached_solve(i, p0, sh, rtol=1e-4)
                case_fi = r["case_fi"]
            for mid in mats:
                q = prof(mid, nozzle, lh)
                option(i, q, sh, _material_safety(case_fi, p_ref_s, q) if case_fi else None, gm_for(i))

    own_search = ([m for m in mats[1:] if m in owned] + [m for m in ("pla", "petg") if m in mats[1:] and m not in owned])[:OWN_SEARCH_MATERIALS]

    def _run_climb(i: int, q: Profile, zones=None, solid_known_ok: bool = False) -> dict:
        return climb(cases, poses[i].up, q, target, mesh, zones=zones, precond_cache=precond_cache,
                     eval_fn=climb_eval(i, q), solid_known_ok=solid_known_ok)

    def refine(i: int, nozzle: str, own_climbs: bool = True) -> None:
        """The greedy wall / top / infill search (api._climb, full anisotropic solves) for one pose: the lightest
        recipe that reaches the target, for the first material; other materials reuse its verified solve when the
        same recipe is enough for them, else (Extended/Complete) get their own search (sharing the solve cache)."""
        pose_up = [round(float(x), 6) for x in poses[i].up]
        for lh in layer_heights(goal, float(nozzle), lock.get("layer_height"), depth):
            p0 = prof(mats[0], nozzle, lh)
            known_ok_p0 = any(c["up"] == pose_up and c["nozzle"] == nozzle and c["layer_height"] == round(lh, 2)
                              and c["material"] == mats[0] and c["meets_target"] for c in cands)
            ref = _run_climb(i, p0, solid_known_ok=known_ok_p0)
            sh = _locked_shells(Shells(walls=ref["walls"], infill=ref["infill"], top=ref["top"], bottom=ref["bottom"]), lock)
            r, p_ref_s = cached_solve(i, p0, sh, rtol=1e-4)
            case_fi = r["case_fi"]
            for mid in mats:
                q = prof(mid, nozzle, lh)
                sf = _material_safety(case_fi, p_ref_s, q)
                if sf >= target or depth == "fast" or mid == mats[0] or not own_climbs or mid not in own_search:
                    option(i, q, sh, sf, gm_for(i))
                    continue
                known_ok_q = any(c["up"] == pose_up and c["nozzle"] == nozzle and c["layer_height"] == round(lh, 2)
                                 and c["material"] == mid and c["meets_target"] for c in cands)
                own = _run_climb(i, q, solid_known_ok=known_ok_q)
                sh2 = _locked_shells(Shells(walls=own["walls"], infill=own["infill"], top=own["top"], bottom=own["bottom"]), lock)
                r2, p_ref2 = cached_solve(i, q, sh2, rtol=1e-4)
                option(i, q, sh2, _material_safety(r2["case_fi"], p_ref2, q), gm_for(i))

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
                r, p_ref_s = cached_solve(i, p0, sh, rtol=1e-4)
                for mid in mats:
                    q = prof(mid, nozzle, lh)
                    option(i, q, sh, _material_safety(r["case_fi"], p_ref_s, q), gm_for(i))

    nozzle_notes: list[dict] = []

    def result(partial: bool = False) -> dict:
        """The options from the candidates checked so far (pick_options copies them, so this can run again)."""
        picks = pick_options(cands, goal, owned)
        feasible = bool(picks)
        out = {"goal": goal, "depth": depth, "target": target, "feasible": feasible, "skipped": list(skipped),
               "materials_skipped": mat_skipped, "nozzles_skipped": list(nozzle_notes), "candidates_checked": len(cands)}
        if partial:
            out["partial"] = True
        if ended_at:
            out["ended_early"] = True
            out["ended_at"] = ended_at[0]
        if not feasible:
            best = max(cands, key=lambda c: c["safety"] or 0.0) if cands else None
            if best is not None:
                picks = [dict(best, badge="Best reachable", recommended=True, below_target=True)]
            out["ways_out"] = ways_out(change, bool(lock.get("material")) or bool(materials), best and best["safety"], target)
            if ended_at or partial:  # not everything was tried: never "impossible" (plan §10.1)
                out["ways_out"].insert(0, {"id": "continue", "label": "Continue the search"})
                out["explanation"] = (f"Search ended early: none of the options checked so far reaches ×{target:g}"
                                      + (f" (best ×{best['safety']:.1f})" if best and best["safety"] else "")
                                      + ". Continue the search to try the rest.")
            else:
                out["explanation"] = (f"None of the options we checked reached ×{target:g}. Best: ×{best['safety']:.1f}" if best and best["safety"]
                                      else "Nothing could be checked.")
        elif ended_at:
            out["explanation"] = "Search ended early: these are the best options it found in the time. Continue the search to try the rest."
        for o in picks:
            o.pop("reinforcement_kind", None)
            pros_cons(o, picks[0], mat_cons)
            o["settings"] = _slicer_settings(o)
        out["options"] = picks
        return out

    def emit() -> None:
        """The best options so far, for "Stop & show best so far"."""
        if cands and progress.wants_partial():
            progress.partial(result(partial=True))

    # Phase A: every pose once (cheap). Phase B: the recipe search only on the most promising poses.
    for i in range(len(poses)):
        if i and over("strength"):  # at least one pose, so there is something to show
            break
        progress.report(0.15 + 0.30 * i / max(len(poses), 1), "Checking strength" if strength_on else "Measuring support")
        scan(i, noz[0])
    emit()

    ranked_poses = list(range(len(poses)))
    if cands:
        ref_c = min(cands, key=lambda c: c["minutes"])
        ranked_poses = sorted(range(len(poses)), key=lambda i: min(
            (goal_cost(c, goal, ref_c) if c["meets_target"] else 1e6 - (c["safety"] or 0.0)
             for c in cands if c["up"] == [round(float(x), 6) for x in poses[i].up]), default=1e9))

    if strength_on and not recipe_fixed:
        top = ranked_poses[:REFINE_POSES[depth]]
        for k, i in enumerate(top):
            if over("settings"):
                break
            progress.report(0.45 + 0.25 * k / len(top), "Trying walls, top layers and infill")
            refine(i, noz[0], own_climbs=k == 0)  # other materials' own search only on the best pose
            emit()
        if cands:
            ref_c = min(cands, key=lambda c: c["minutes"])
            ranked_poses = sorted(range(len(poses)), key=lambda i: min(
                (goal_cost(c, goal, ref_c) if c["meets_target"] else 1e6 - (c["safety"] or 0.0)
                 for c in cands if c["up"] == [round(float(x), 6) for x in poses[i].up]), default=1e9))

    # ---------- 4. Bigger nozzles: only if the part still prints with them and the gain is significant. ----------
    if bigger and cands and not over("nozzles"):
        best_i = _pose_index(poses, min((c for c in cands if c["meets_target"]), key=lambda c: c["minutes"], default=cands[0])["up"])
        base_rows = [c for c in cands if c["up"] == [round(float(x), 6) for x in poses[best_i].up] and c["nozzle"] == noz[0]]
        nozzle_ctx: dict = {"oriented": _oriented(best_i)[0]}
        for idx_n, n in enumerate(bigger):
            if idx_n and over("nozzles"):
                break
            progress.report(0.70 + 0.08 * idx_n / max(len(bigger), 1), "Trying nozzles")
            lost = _nozzle_blockers(mesh, poses[best_i].up, prof(mats[0], n), bed, moving, ctx=nozzle_ctx)
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
        emit()

    # ---------- 7. Local reinforcements (Complete depth, and design-only at every depth). ----------
    run_zones = strength_on and depth == "complete" and change in ("slicer", "both")
    run_design = strength_on and ((depth == "complete" and change in ("design", "both")) or change == "design")
    if (run_zones or run_design) and not over("reinforce"):
        progress.report(0.78, "Testing local reinforcements")
        reinf_poses = ranked_poses[:REINF_POSES[depth]]
        added = _test_reinforcements(
            mesh, cases, poses, reinf_poses, mats, noz[0], goal, depth, target, lock,
            prof, option, gm_for, cached_solve, _run_climb, cands,
            run_zones=run_zones, run_design=run_design, recipe_fixed=recipe_fixed,
            bed=bed, moving=moving, zone_info=zone_info, unit_shape=unit_shape,
            precond_cache=precond_cache,
        )
        if depth == "complete" and change == "slicer":
            skipped.append({"what": "design changes",
                            "why": "Slicer-only mode: only local modifier zones were tested (allow design changes to also test hidden gaps, fillets and local thickening)."})
        if added == 0:
            skipped.append({"what": "reinforcements",
                            "why": "Tested local reinforcements at the weak spots, but none improved on the standard print here."})
    elif strength_on and change == "both" and depth != "complete":
        skipped.append({"what": "design additions",
                        "why": "Local reinforcements and design additions run in Complete depth."})

    # ---------- 8. Pick 2–4 distinct options; honest result when nothing reaches the target. ----------
    progress.report(0.92, "Picking options")
    out = result()
    progress.report(1.0, "Picking options")
    return out


# ------------------------------------------------------------------ Complete / design-only reinforcements


def _test_reinforcements(mesh, cases, poses, pose_indices: list[int], mats: list[str], nozzle: str, goal: str,
                         depth: str, target: float, lock: dict, prof, option, gm_for, cached_solve, run_climb,
                         cands: list[dict], *, run_zones: bool, run_design: bool, recipe_fixed: bool,
                         bed, moving: bool, zone_info, unit_shape, precond_cache: dict) -> int:
    """Test local reinforcements in voxel space on ``pose_indices`` (best 1–2 poses):
    (a) modifier zones, (b) hidden gaps, (c) inside-corner fillets & local thickening.
    Only winners get real edit meshes, and every winner is re-checked in voxel space before being added.
    Returns the number of verified winning reinforced candidates added."""
    from .features import concave_edges
    from .placement import rotation_to_z
    from .strength import Shells, find_zones

    tier = "full" if depth == "complete" else "cheap"
    lh = layer_heights(goal, float(nozzle), lock.get("layer_height"), "fast")[0]
    p0 = prof(mats[0], nozzle, lh)
    model = cases[0].model
    edges_cache = concave_edges(mesh) if run_design else []
    added = 0
    base_snapshot = list(cands)  # unreinforced candidates for honest before/after comparison

    for k, i in enumerate(pose_indices):
        progress.report(0.78 + 0.14 * k / max(len(pose_indices), 1), "Testing local reinforcements")
        up = poses[i].up
        pose_up = [round(float(x), 6) for x in up]
        pose_base = [c for c in base_snapshot if c["up"] == pose_up and c["nozzle"] == nozzle]
        if not pose_base:
            continue
        # Representative recipe on this pose: fixed baseline in design-only, else best unreinforced for mats[0].
        base_m0 = [c for c in pose_base if c["material"] == mats[0]] or pose_base
        ok_m0 = [c for c in base_m0 if c["meets_target"]]
        ref_row = min(ok_m0, key=lambda c: c["total_g"]) if ok_m0 else max(base_m0, key=lambda c: c["safety"] or 0.0)
        sh_best = Shells(walls=ref_row["walls"], infill=ref_row["infill"], top=ref_row["top"], bottom=ref_row["bottom"])
        sh_light = _locked_shells(Shells(walls=2, infill=0.10, top=3, bottom=3), lock)
        # Get stress field on this pose (cache hit from scan/refine).
        r_stress, p_ref_s = cached_solve(i, p0, sh_best, rtol=1e-4)
        fi = r_stress["fi"]
        case_idx = [c.number for c in cases].index(r_stress["case"])
        ignore = cases[case_idx].ignore
        weak_spot = np.asarray(r_stress["critical_point"], float)
        hot_spots = _hot_spots_of(model, fi, ignore)

        # ---- (a) Modifier zones (slicer-only or both, Complete depth) ----
        if run_zones:
            boxes = find_zones(model, fi, ignore, up, 2, 0.10)
            if boxes:
                z_res = run_climb(i, p0, zones=boxes, solid_known_ok=bool(ok_m0))
                z_walls_max = max((z["walls"] for z in z_res.get("zones", [])), default=z_res["walls"])
                z_infill_max = max((z["infill"] for z in z_res.get("zones", [])), default=z_res["infill"])
                if z_walls_max > z_res["walls"] or z_infill_max > z_res["infill"] + 1e-6:
                    z_objs = tuple(replace(b, walls=max(z_walls_max, z_res["walls"]),
                                           infill=max(z_infill_max, z_res["infill"])) for b in boxes)
                    sh_z = _locked_shells(Shells(walls=z_res["walls"], infill=z_res["infill"],
                                                 top=z_res["top"], bottom=z_res["bottom"], zones=z_objs), lock)
                    rz, p_rz = cached_solve(i, p0, sh_z, rtol=1e-4)
                    z_dicts = [_zone_dict(zone_info, z, mesh, up, bed, moving, kind="stronger") for z in sh_z.zones]
                    label = f"{len(z_dicts)} stronger local zone{'s' if len(z_dicts) > 1 else ''}"
                    detail = f"{z_walls_max} walls, {z_infill_max:.0%} infill in stressed areas"
                    added += _emit_if_better(
                        i, mats, nozzle, lh, prof, sh_z, rz["case_fi"], p_rz, gm_for(i), option,
                        base_snapshot, pose_up, goal, target,
                        kind="modifier_zone", label=label, detail=detail, zones=z_dicts, edits=[],
                    )

        # ---- (b) Hidden gaps (voids forcing solid inner walls) ----
        if run_design:
            test_shells = [sh_best] if (recipe_fixed or sh_best == sh_light) else [sh_best, sh_light]
            for n_pins in GAP_PIN_COUNTS[tier]:
                voids = _find_hidden_gaps(model, fi, ignore, up, p0, max_pins=n_pins)
                if not voids:
                    break
                for sh_try in test_shells:
                    sh_v = replace(sh_try, zones=tuple(voids))
                    rv, p_rv = cached_solve(i, p0, sh_v, rtol=1e-4)
                    # Check if any material improves before building export zone dicts & cut meshes.
                    if _any_material_improves(mats, nozzle, lh, prof, sh_v, rv["case_fi"], p_rv,
                                              gm_for(i), base_snapshot, pose_up, goal, target):
                        z_dicts = [_zone_dict(zone_info, z, mesh, up, bed, moving, kind="void") for z in voids]
                        gap_edits = _hidden_gap_edits(voids, up, unit_shape)
                        n = len(voids)
                        label = f"{n} hidden reinforcing gap{'s' if n > 1 else ''}"
                        detail = "Internal pin voids that force solid inner walls through the stressed core"
                        added += _emit_if_better(
                            i, mats, nozzle, lh, prof, sh_v, rv["case_fi"], p_rv, gm_for(i), option,
                            base_snapshot, pose_up, goal, target,
                            kind="hidden_gap", label=label, detail=detail, zones=z_dicts, edits=gap_edits,
                        )
                if len(voids) < n_pins:
                    break  # no room for more pins on this pose

        # ---- (c) Inside-corner fillets & local thickening (voxel screen -> winners get edit mesh + re-check) ----
        if run_design:
            r0 = rotation_to_z(up)
            bed_z = float((mesh.triangles.reshape(-1, 3) @ r0.T)[:, 2].min())
            test_shells = [sh_best] if (recipe_fixed or sh_best == sh_light) else [sh_best, sh_light]
            u_base = r_stress.get("_u_all")

            # C1: Inside-corner fillets
            added += _try_fillets(
                mesh, cases, edges_cache, i, up, pose_up, bed_z, weak_spot, hot_spots,
                mats, nozzle, lh, p0, test_shells, u_base, FILLET_SIZES[tier],
                prof, gm_for, option, base_snapshot, goal, target,
            )

            # C2: Local thickening
            added += _try_thickening(
                mesh, cases, i, up, pose_up, bed_z, fi, ignore, weak_spot, hot_spots,
                mats, nozzle, lh, p0, test_shells, u_base, THICKEN_SIZES[tier],
                prof, gm_for, option, base_snapshot, goal, target, bed, moving,
            )
    return added


def _zone_dict(zone_info, z, mesh, up, bed, moving: bool, kind: str) -> dict:
    d = zone_info(z, mesh, up, bed, moving)
    d["kind"] = kind
    return d


def _hidden_gap_edits(voids: list, up, unit_shape) -> list[dict]:
    """Closed cylinder cut pieces (part coordinates, float32 bytes) for winning hidden gaps."""
    from .placement import rotation_to_z

    if not voids or unit_shape is None:
        return []
    r0 = rotation_to_z(up)
    cyl = unit_shape("cylinder")
    pieces = []
    for z in voids:
        lo, hi = np.asarray(z.lo, float), np.asarray(z.hi, float)
        tris = (cyl * (hi - lo) / 2 + (lo + hi) / 2) @ r0  # build frame -> part frame
        pieces.append(tris)
    n = len(pieces)
    return [{
        "id": "hidden-gaps",
        "rule": "hidden_gap",
        "title": f"Add {n} hidden reinforcing gap{'s' if n > 1 else ''}",
        "description": "Thin internal pin voids inside the stressed region that force the slicer to print solid walls through the core.",
        "op": "cut",
        "param": None,
        "mesh": np.concatenate(pieces).astype("<f4").tobytes(),
        "pieces": [len(q) for q in pieces],
    }]


def _hot_spots_of(model, fi: np.ndarray, ignore: np.ndarray) -> tuple[np.ndarray, np.ndarray] | None:
    """(points, share) of voxels carrying ≥ 60 % of the worst stress (same convention as api.strength)."""
    worst = float(fi[~ignore].max(initial=0.0))
    if worst <= 0:
        return None
    hot = (fi >= 0.6 * worst) & (~ignore)
    if not hot.any():
        return None
    return model.grid.centers(model.elems[hot]), fi[hot] / worst


def _find_hidden_gaps(model, fi: np.ndarray, ignore: np.ndarray, up, p: Profile, max_pins: int = 3) -> list:
    """Find up to ``max_pins`` upright hidden pin-void locations strictly inside the stressed core (pure voxel space)."""
    from .placement import rotation_to_z
    from .strength import Zone

    worst = float(fi[~ignore].max(initial=0.0))
    if worst <= 0:
        return []
    hot = np.flatnonzero((fi >= 0.55 * worst) & (~ignore))
    if not len(hot):
        return []
    grid = model.grid
    h = grid.h
    solid = grid.solid
    shape = np.array(solid.shape, dtype=int)
    r0 = rotation_to_z(up)
    up_u = np.asarray(up, float) / np.linalg.norm(up)
    a = np.cross(up_u, [1.0, 0, 0] if abs(up_u[0]) < 0.9 else [0, 1.0, 0])
    a /= np.linalg.norm(a)
    b = np.cross(up_u, a)
    side_dirs = np.array([np.cos(t) * a + np.sin(t) * b for t in np.arange(8) * np.pi / 4])
    cover_xy = VOID_COVER_MM + VOID_MM / 2
    min_spacing = max(2.2 * p.line_width * 2, 2.5)
    step = h / 2

    def is_inside(pts: np.ndarray) -> np.ndarray:
        idx = grid.index_of(pts)
        ok = np.all((idx >= 0) & (idx < shape), axis=1)
        out = np.zeros(len(pts), dtype=bool)
        out[ok] = solid[tuple(idx[ok].T)]
        return out

    order = hot[np.argsort(-fi[hot])]
    centers_part = grid.centers(model.elems[order])
    centers_build = centers_part @ r0.T
    chosen_xy: list[np.ndarray] = []
    zones: list = []
    max_steps = int(np.ceil(float((shape * h).max()) / step)) + 2

    for idx_k in range(min(len(order), 120)):
        if len(zones) >= max_pins:
            break
        pt_b = centers_build[idx_k]
        if any(float(np.hypot(pt_b[0] - c[0], pt_b[1] - c[1])) < min_spacing for c in chosen_xy):
            continue
        pt_p = centers_part[idx_k]
        # Walk up and down along build Z to find solid thickness at this (x, y).
        t_pos = np.arange(1, max_steps) * step
        up_in = is_inside(pt_p + t_pos[:, None] * up_u)
        dn_in = is_inside(pt_p - t_pos[:, None] * up_u)
        n_up = int(np.argmin(up_in)) if not up_in.all() else len(up_in)
        n_dn = int(np.argmin(dn_in)) if not dn_in.all() else len(dn_in)
        z_top = pt_b[2] + n_up * step
        z_bot = pt_b[2] - n_dn * step
        z_lo = z_bot + VOID_COVER_MM
        z_hi = z_top - VOID_COVER_MM
        if z_hi - z_lo < 1.5:
            continue
        # Verify side clearance at bottom, middle, and top of the pin so it stays hidden.
        z_checks = np.array([z_lo, 0.5 * (z_lo + z_hi), z_hi])
        ring_ok = True
        for zc in z_checks:
            c_part = np.array([pt_b[0], pt_b[1], zc]) @ r0
            ring_pts = c_part[None, :] + side_dirs * cover_xy
            if not is_inside(ring_pts).all():
                ring_ok = False
                break
        if not ring_ok:
            continue
        rad = VOID_MM / 2
        chosen_xy.append(pt_b[:2])
        zones.append(Zone(
            (round(float(pt_b[0] - rad), 2), round(float(pt_b[1] - rad), 2), round(float(z_lo), 2)),
            (round(float(pt_b[0] + rad), 2), round(float(pt_b[1] + rad), 2), round(float(z_hi), 2)),
            walls=0, infill=0.0, void=True, shape="cylinder",
        ))
    return zones


def _voxel_fillet_grid(model, edges: list[dict], weak_spot: np.ndarray, hot_spots, up, size: float,
                       bed_z: float) -> tuple[np.ndarray, np.ndarray] | None:
    """Screen an inside-corner fillet in pure voxel space (no triangle mesh or booleans).
    Returns ``(new_solid, new_fill)`` on the same grid if any empty voxels are filled, else None."""
    from .features import CHAMFER_REACH_VOXELS, HOT_REACH_VOXELS, MAX_STRESS_CORNERS, _connected, _edge_heat
    from .placement import rotation_to_z

    if not edges:
        return None
    grid = model.grid
    h = grid.h
    mids = np.array([(e["a"] + e["b"]) / 2 for e in edges])
    dist = np.linalg.norm(mids - weak_spot, axis=1)
    if float(dist.min()) > max(CHAMFER_REACH_VOXELS * h, size * 2):
        return None
    corners = [_connected(edges, [int(np.argmin(dist))])]
    if hot_spots is not None:
        heat = _edge_heat(edges, *hot_spots, HOT_REACH_VOXELS * h)
        left = set(range(len(edges))) - set(corners[0])
        others = []
        while left:
            c = _connected(edges, [min(left)])
            left -= set(c)
            if heat[c].max() > 0:
                others.append((heat[c].max(), c))
        corners += [c for _, c in sorted(others, key=lambda o: -o[0])[:MAX_STRESS_CORNERS - 1]]
    sel = [edges[k] for c in corners for k in c][:120]
    if not sel:
        return None

    r0 = rotation_to_z(up)
    shape = np.array(grid.solid.shape, dtype=int)
    add = np.zeros(grid.solid.shape, dtype=bool)
    d_eff = size * 1.1  # round fillet has slightly more reach than a flat chamfer of the same plastic
    for e in sel:
        a_pt, b_pt = e["a"], e["b"]
        ev = b_pt - a_pt
        length = float(np.linalg.norm(ev))
        if length < 1e-6:
            continue
        axis = ev / length
        t1, t2 = e["t1"], e["t2"]
        n1, n2 = e["n1"], e["n2"]
        dot12 = float(np.dot(t1, t2))
        det = 1.0 - dot12 * dot12
        if det < 0.05:
            continue
        pts_box = np.array([a_pt, b_pt, a_pt + t1 * d_eff, b_pt + t1 * d_eff, a_pt + t2 * d_eff, b_pt + t2 * d_eff])
        i0 = np.maximum(np.floor((pts_box.min(axis=0) - h - grid.origin) / h).astype(int), 0)
        i1 = np.minimum(np.ceil((pts_box.max(axis=0) + h - grid.origin) / h).astype(int), shape)
        if np.any(i1 <= i0):
            continue
        sub_empty = ~grid.solid[i0[0]:i1[0], i0[1]:i1[1], i0[2]:i1[2]]
        if not sub_empty.any():
            continue
        local_idx = np.argwhere(sub_empty) + i0
        pts = grid.centers(local_idx)
        rel = pts - a_pt
        s = rel @ axis
        ok = (s >= -0.25 * h) & (s <= length + 0.25 * h)
        if not ok.any():
            continue
        pts, local_idx, rel, s = pts[ok], local_idx[ok], rel[ok], s[ok]
        v = rel - s[:, None] * axis
        open_side = (v @ n1 >= -0.25 * h) & (v @ n2 >= -0.25 * h)
        if not open_side.any():
            continue
        pts, local_idx, v = pts[open_side], local_idx[open_side], v[open_side]
        vt1, vt2 = v @ t1, v @ t2
        c1 = (vt1 - dot12 * vt2) / det
        c2 = (vt2 - dot12 * vt1) / det
        in_wedge = (c1 >= -0.2 * h) & (c2 >= -0.2 * h) & (c1 + c2 <= d_eff + 0.25 * h)
        if not in_wedge.any():
            continue
        pts_hit = pts[in_wedge]
        above = (pts_hit @ r0.T)[:, 2] >= bed_z - 0.1
        if above.any():
            hit = local_idx[in_wedge][above]
            add[tuple(hit.T)] = True

    if not add.any():
        return None
    new_solid = grid.solid | add
    old_fill = grid.solid.astype(float) if grid.fill is None else grid.fill
    new_fill = np.where(add, 1.0, old_fill)
    return new_solid, new_fill


def _voxel_thicken_grid(model, cases, fi: np.ndarray, ignore: np.ndarray, up, grow: float,
                        bed_z: float) -> tuple[np.ndarray, np.ndarray, tuple[int, int, int], float] | None:
    """Screen local thickening around the top stress cluster in pure voxel space (no mesh booleans)."""
    from scipy.ndimage import binary_dilation

    from .placement import rotation_to_z

    worst = float(fi[~ignore].max(initial=0.0))
    if worst <= 0:
        return None
    hot = (fi >= 0.6 * worst) & (~ignore)
    if not hot.any():
        return None
    grid = model.grid
    h = grid.h
    steps = max(1, int(np.ceil(grow / h)))
    pad = steps
    old_shape = np.array(grid.solid.shape, dtype=int)
    new_shape = old_shape + 2 * pad
    sl = tuple(slice(pad, pad + int(old_shape[k])) for k in range(3))

    base_solid = np.zeros(tuple(new_shape), dtype=bool)
    base_solid[sl] = grid.solid
    old_fill = grid.solid.astype(float) if grid.fill is None else grid.fill
    base_fill = np.zeros(tuple(new_shape), dtype=float)
    base_fill[sl] = old_fill

    # Seed around stressed voxels (expand by 2 cubes along the part so the whole weak cross-section grows).
    seed = np.zeros(tuple(new_shape), dtype=bool)
    hot_idx = model.elems[hot] + pad
    seed[tuple(hot_idx.T)] = True
    seed = binary_dilation(seed, structure=np.ones((3, 3, 3), dtype=bool), iterations=2) & base_solid

    # Keep clear of held/loaded boundary voxels so supports/loads never get buried.
    bc_grid = np.zeros(tuple(new_shape), dtype=bool)
    for case in cases:
        ign_idx = model.elems[case.ignore] + pad
        bc_grid[tuple(ign_idx.T)] = True
    bc_guard = binary_dilation(bc_grid, structure=np.ones((3, 3, 3), dtype=bool), iterations=1)

    grown = binary_dilation(seed, structure=np.ones((3, 3, 3), dtype=bool), iterations=steps)
    add = grown & (~base_solid) & (~bc_guard)
    if not add.any():
        return None

    # Keep everything above the bed plane for build direction `up`.
    r0 = rotation_to_z(up)
    new_origin = grid.origin - pad * h
    add_idx = np.argwhere(add)
    pts = new_origin + (add_idx + 0.5) * h
    above = (pts @ r0.T)[:, 2] >= bed_z - 0.1
    if not above.any():
        return None
    add_clean = np.zeros(tuple(new_shape), dtype=bool)
    add_clean[tuple(add_idx[above].T)] = True

    new_solid = base_solid | add_clean
    new_fill = np.where(add_clean, 1.0, base_fill)
    added_mm3 = float(add_clean.sum()) * h**3
    return new_solid, new_fill, (pad, pad, pad), added_mm3


def _unpack_edit_pieces(edit: dict) -> list[np.ndarray]:
    raw = np.frombuffer(edit["mesh"], dtype="<f4").reshape(-1, 3, 3).astype(float)
    out, i = [], 0
    for n in edit.get("pieces", []):
        if n > 0:
            out.append(raw[i : i + n])
            i += n
    return out


def _try_fillets(mesh, cases, edges: list[dict], i: int, up, pose_up: list[float], bed_z: float,
                 weak_spot: np.ndarray, hot_spots, mats: list[str], nozzle: str, lh: float, p0: Profile,
                 test_shells: list, u_base, sizes: tuple[float, ...], prof, gm_for, option,
                 base_snapshot: list[dict], goal: str, target: float) -> int:
    """Screen inside-corner fillets in voxel space; only winners get edit meshes and a full re-check."""
    from .features import _stress_chamfers
    from .strength import augment_cases, evaluate_cases, map_displacement, rasterize_edit_onto_grid

    model = cases[0].model
    orig_vol = abs(float(mesh.volume()))
    added = 0
    winners: list[tuple[float, object]] = []

    # Stage 1: pure voxel-space screening (zero edit meshes built).
    for size in sizes:
        res_grid = _voxel_fillet_grid(model, edges, weak_spot, hot_spots, up, size, bed_z)
        if res_grid is None:
            continue
        new_solid, new_fill = res_grid
        added_mm3 = float((new_solid & ~model.grid.solid).sum()) * model.grid.h**3
        aug_cases, node_map = augment_cases(cases, new_solid, new_fill, (0, 0, 0))
        pc_local: dict = {}
        x0_aug = [map_displacement(aug_cases[0].model.n_nodes, node_map, u) for u in u_base] if u_base else None
        for sh_try in test_shells:
            r_scr = evaluate_cases(aug_cases, up, p0, sh_try, x0=x0_aug, rtol=1e-3, precond_cache=pc_local)
            if _any_material_improves(mats, nozzle, lh, prof, sh_try, r_scr["case_fi"], p0,
                                      gm_for(i, aug_cases[0].model, orig_vol + added_mm3),
                                      base_snapshot, pose_up, goal, target, min_gain=0.05):
                winners.append((size, sh_try))

    # Stage 2: ONLY winners get real edit meshes and are re-checked with the exact rasterised edit.
    built_edits: dict[float, tuple[dict, list, np.ndarray, float]] = {}
    for size, sh_try in winners:
        if size not in built_edits:
            edits = _stress_chamfers(mesh, weak_spot, model.grid.h, size, hot_spots=hot_spots, up=up,
                                     max_overhang_deg=p0.material["max_overhang_deg"], repeated_load=p0.repeated_load)
            if not edits:
                built_edits[size] = None
                continue
            edit = dict(edits[0])
            edit.pop("_advice", None)
            pieces = _unpack_edit_pieces(edit)
            if not pieces:
                built_edits[size] = None
                continue
            chk_solid, chk_fill, pad_lo, real_add_mm3 = rasterize_edit_onto_grid(model.grid, pieces)
            chk_cases, chk_map = augment_cases(cases, chk_solid, chk_fill, pad_lo)
            built_edits[size] = (edit, chk_cases, chk_map, real_add_mm3)
        entry = built_edits[size]
        if entry is None:
            continue
        edit, chk_cases, chk_map, real_add_mm3 = entry
        x0_chk = [map_displacement(chk_cases[0].model.n_nodes, chk_map, u) for u in u_base] if u_base else None
        r_chk = evaluate_cases(chk_cases, up, p0, sh_try, x0=x0_chk, rtol=1e-4)
        gm_chk = gm_for(i, chk_cases[0].model, orig_vol + real_add_mm3)
        added += _emit_if_better(
            i, mats, nozzle, lh, prof, sh_try, r_chk["case_fi"], p0, gm_chk, option,
            base_snapshot, pose_up, goal, target,
            kind="fillet", label=edit["title"], detail=edit["description"], zones=[], edits=[edit],
        )
    return added


def _try_thickening(mesh, cases, i: int, up, pose_up: list[float], bed_z: float, fi: np.ndarray,
                    ignore: np.ndarray, weak_spot: np.ndarray, hot_spots, mats: list[str], nozzle: str,
                    lh: float, p0: Profile, test_shells: list, u_base, grows: tuple[float, ...], prof,
                    gm_for, option, base_snapshot: list[dict], goal: str, target: float, bed, moving: bool) -> int:
    """Screen local thickening in voxel space; only winners get edit meshes and a full re-check."""
    from .features import _thicker_weak_spot
    from .strength import augment_cases, evaluate_cases, map_displacement, rasterize_edit_onto_grid

    model = cases[0].model
    orig_vol = abs(float(mesh.volume()))
    added = 0
    winners: list[tuple[float, object]] = []

    # Stage 1: pure voxel-space screening (zero edit meshes built).
    for grow in grows:
        res_grid = _voxel_thicken_grid(model, cases, fi, ignore, up, grow, bed_z)
        if res_grid is None:
            continue
        new_solid, new_fill, pad_lo, added_mm3 = res_grid
        aug_cases, node_map = augment_cases(cases, new_solid, new_fill, pad_lo)
        pc_local: dict = {}
        x0_aug = [map_displacement(aug_cases[0].model.n_nodes, node_map, u) for u in u_base] if u_base else None
        for sh_try in test_shells:
            r_scr = evaluate_cases(aug_cases, up, p0, sh_try, x0=x0_aug, rtol=1e-3, precond_cache=pc_local)
            if _any_material_improves(mats, nozzle, lh, prof, sh_try, r_scr["case_fi"], p0,
                                      gm_for(i, aug_cases[0].model, orig_vol + added_mm3),
                                      base_snapshot, pose_up, goal, target, min_gain=0.05):
                winners.append((grow, sh_try))

    # Stage 2: ONLY winners get real edit meshes and are re-checked with the exact rasterised edit.
    built_edits: dict[float, tuple[dict, list, np.ndarray, float] | None] = {}
    for grow, sh_try in winners:
        if grow not in built_edits:
            edit = _build_thicken_edit(mesh, weak_spot, grow, hot_spots, up, p0, bed, moving)
            if edit is None:
                built_edits[grow] = None
                continue
            pieces = _unpack_edit_pieces(edit)
            if not pieces:
                built_edits[grow] = None
                continue
            chk_solid, chk_fill, pad_lo, real_add_mm3 = rasterize_edit_onto_grid(model.grid, pieces)
            chk_cases, chk_map = augment_cases(cases, chk_solid, chk_fill, pad_lo)
            built_edits[grow] = (edit, chk_cases, chk_map, real_add_mm3)
        entry = built_edits[grow]
        if entry is None:
            continue
        edit, chk_cases, chk_map, real_add_mm3 = entry
        x0_chk = [map_displacement(chk_cases[0].model.n_nodes, chk_map, u) for u in u_base] if u_base else None
        r_chk = evaluate_cases(chk_cases, up, p0, sh_try, x0=x0_chk, rtol=1e-4)
        gm_chk = gm_for(i, chk_cases[0].model, orig_vol + real_add_mm3)
        added += _emit_if_better(
            i, mats, nozzle, lh, prof, sh_try, r_chk["case_fi"], p0, gm_chk, option,
            base_snapshot, pose_up, goal, target,
            kind="thicken", label=edit["title"], detail=edit["description"], zones=[], edits=[edit],
        )
    return added


def _build_thicken_edit(mesh, weak_spot: np.ndarray, grow: float, hot_spots, up, p0: Profile,
                        bed, moving: bool) -> dict | None:
    """Build a local thickening edit mesh via features._thicker_weak_spot, falling back to
    additions.thicken_walls when the weak spot sits on a thin wall."""
    from .features import _thicker_weak_spot

    edits = _thicker_weak_spot(mesh, weak_spot, grow, hot_spots=hot_spots, up=up)
    if edits:
        edit = dict(edits[0])
        edit.pop("_advice", None)
        return edit
    from .additions import thicken_walls
    from .checks import orient
    from .placement import print_rotation

    rot, _ = print_rotation(mesh, up, bed, moving)
    turned = mesh.triangles.reshape(-1, 3) @ rot.T
    lo, hi = turned.min(axis=0), turned.max(axis=0)
    base_shift = np.array([(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, lo[2]])
    walls = thicken_walls(orient(mesh, rot), p0, lines=max(3.0, 2.0 + grow / max(p0.line_width, 0.2)), max_layers=80)
    if not walls:
        return None
    pieces = [(q + base_shift) @ rot for w in walls for q in w["pieces"] if np.isfinite(q).all()]
    if not pieces:
        return None
    return {
        "id": "thicken-walls",
        "rule": "thin_wall",
        "title": f"Thicken thin walls by about {grow:.1f} mm",
        "description": "Grows thin load-bearing walls so they print with solid perimeters.",
        "op": "add",
        "param": "thicken_mm",
        "mesh": np.concatenate(pieces).astype("<f4").tobytes(),
        "pieces": [len(q) for q in pieces],
    }


def _material_improves(mid: str, sf: float, part_g: float, base_snapshot: list[dict], pose_up: list[float],
                       goal: str, target: float, min_gain: float = REINF_MIN_SAFETY_GAIN) -> bool:
    """True only if this reinforced candidate genuinely beats unreinforced options for ``mid``."""
    same_mat = [c for c in base_snapshot if c["material"] == mid]
    same_pose = [c for c in same_mat if c["up"] == pose_up]
    ref_list = same_pose or same_mat
    if not ref_list:
        return True
    ok_mat = [c for c in same_mat if c["meets_target"]]
    ok_pose = [c for c in same_pose if c["meets_target"]]
    best_sf_pose = max((c["safety"] or 0.0 for c in ref_list), default=0.0)
    best_sf_all = max((c["safety"] or 0.0 for c in same_mat), default=0.0)
    # 1. Reaches target when nothing unreinforced did (on this pose or overall for this material)
    if sf >= target and not ok_mat:
        return True
    if sf < target:
        return sf >= best_sf_all * (1.0 + min_gain)
    # 2. Lighter while meeting target (comparing part_g on the same pose, since support_g is identical)
    if ok_pose:
        lightest_pose = min(c["part_g"] for c in ok_pose)
        if part_g <= lightest_pose * (1.0 - REINF_MIN_GRAM_SAVE) and lightest_pose - part_g >= REINF_MIN_GRAM_SAVE_ABS:
            return True
    # 3. Meaningfully stronger on this pose (without blowing up plastic on non-Strong goals)
    base_g = min((c["part_g"] for c in ok_pose), default=min(c["part_g"] for c in ref_list))
    if sf >= best_sf_pose * (1.0 + min_gain):
        if goal == "strong" or part_g <= base_g * (1.0 + REINF_MAX_EXTRA_PLASTIC):
            return True
    return False


def _any_material_improves(mats: list[str], nozzle: str, lh: float, prof, sh, case_fi, p_ref: Profile,
                           gm_fn, base_snapshot: list[dict], pose_up: list[float], goal: str,
                           target: float, min_gain: float = REINF_MIN_SAFETY_GAIN) -> bool:
    for mid in mats:
        q = prof(mid, nozzle, lh)
        sf = _material_safety(case_fi, p_ref, q)
        g_part, _ = gm_fn(sh, q)
        if _material_improves(mid, sf, g_part, base_snapshot, pose_up, goal, target, min_gain=min_gain):
            return True
    return False


def _emit_if_better(i: int, mats: list[str], nozzle: str, lh: float, prof, sh, case_fi, p_ref: Profile,
                    gm_fn, option, base_snapshot: list[dict], pose_up: list[float], goal: str, target: float,
                    *, kind: str, label: str, detail: str, zones: list[dict], edits: list[dict]) -> int:
    emitted = 0
    for mid in mats:
        q = prof(mid, nozzle, lh)
        sf = _material_safety(case_fi, p_ref, q)
        g_part, _ = gm_fn(sh, q)
        if _material_improves(mid, sf, g_part, base_snapshot, pose_up, goal, target):
            option(i, q, sh, sf, gm_fn, reinforced=True, reinforcement_kind=kind,
                   reinforcements=[{"kind": kind, "label": label, "detail": detail}],
                   zones=zones, edits=edits)
            emitted += 1
    return emitted


# ------------------------------------------------------------------ Helpers


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


def _material_safety(fi, p_ref: Profile, p: Profile) -> float:
    """Safety for another material from the reference solve. Stress doesn't depend on a uniform stiffness change
    (the layer-to-in-plane stiffness ratio is the same for every material), so only the strengths scale.
    Accepts either a single ``(fi_layers_max, fi_material_max)`` pair or a list of pairs (one per load case)."""
    from .strength import material_for

    a, b = material_for(p_ref), material_for(p)
    rz, rxy = a.strength_z / b.strength_z, a.strength_xy / b.strength_xy
    pairs = fi if (isinstance(fi, list) and fi and isinstance(fi[0], (tuple, list))) else [fi]
    worst = max(max(fl * rz, fm * rxy) for fl, fm in pairs)
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


def _nozzle_blockers(mesh, up, p: Profile, bed, moving: bool, ctx: dict | None = None) -> str:
    """Why this nozzle can't print the part (new nozzle problems or lost detail), or "" (decision 7)."""
    from .checks import lost_detail, nozzle_findings, orient
    from .holes import horizontal_holes
    from .placement import print_rotation
    from .slicer import slice_mesh

    c = {} if ctx is None else ctx
    if "oriented" not in c:
        rot, _ = print_rotation(mesh, up, bed, moving)
        c["oriented"] = orient(mesh, rot)
    oriented = c["oriented"]
    if "rules" not in c:
        c["rules"] = load_data("rules")["rules"]
    rules = c["rules"]
    title = lambda f: f.get("title") or rules.get(f["rule"], {}).get("title") or f.get("short_title") or f["rule"]
    if "hholes" not in c:
        c["hholes"] = horizontal_holes(oriented)
    if "ref_issues" not in c:
        ref = nozzle_findings(oriented, replace(p, nozzle="0.4"), max_layers=80, hholes=c["hholes"])
        c["ref_issues"] = {title(f) for f in ref}
    issues = sorted({title(f) for f in nozzle_findings(oriented, p, max_layers=80, hholes=c["hholes"])})
    new = [t for t in issues if t not in c["ref_issues"]]
    if new:
        return new[0][0].lower() + new[0][1:]
    if "detail_layers" not in c:
        c["detail_layers"] = slice_mesh(oriented, 0.2, 80)
    lost = lost_detail(c["detail_layers"], float(p.nozzle))["lost_mm3"]
    return f"about {lost:.0f} mm³ of small detail would be lost" if lost > 1.0 else ""


def _slicer_settings(o: dict) -> dict:
    """The option as OrcaSlicer keys (strings, as the 3MF export writes them)."""
    return {"wall_loops": str(o["walls"]), "top_shell_layers": str(o["top"]), "bottom_shell_layers": str(o["bottom"]),
            "sparse_infill_density": f"{o['infill'] * 100:.0f}%", "layer_height": f"{o['layer_height']:.2f}",
            "nozzle_diameter": o["nozzle"]}
