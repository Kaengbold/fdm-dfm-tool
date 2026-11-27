from __future__ import annotations

import json
from typing import TYPE_CHECKING

import numpy as np

from .. import progress
from ..formats import read_bodies
from ..mesh import Mesh, to_binary_stl
from ..messages import format_message
from ..orientation import rotation_to_z
from ..placement import print_rotation
from ..profile import Profile, load_data, presets
from ..progress import stage
from ..recommend import goal_plan
from ..service import creep_estimate, required_safety
from . import design as _design
from . import heat as _heat_mod
from . import load as _load
from . import orientation as _orient
from . import session as _S
from . import warping as _warp
from . import zones as _zones

if TYPE_CHECKING:
    from ..strength import ReleasedStrain, Shells


STRENGTH_WEIGHT = 1.5  # score penalty per unit of safety factor below 2


# Penalty per halving of safety compared with the strongest direction, by goal: with "strength"
# a twice-as-strong direction beats a lot of support; balanced weighs it against support and time.
STRENGTH_RATIO_WEIGHT = {"strength": 1.5, "balanced": 1.0, "speed": 0.4, "material": 0.4}


MAX_SCORE_GAP = 3.0  # options scoring this much worse than the best are not shown


def _format_safety(value: float) -> str:
    """Avoid implying more precision than the strength estimate supports."""
    if value > 100:
        return "over ×100"
    return f"about ×{value:.0f}" if value >= 10 or round(value, 1) >= 10 else f"about ×{value:.1f}"


def _strength_band(value: float, need: float) -> str:
    if value >= 5:
        return "Plenty of reserve"
    if value >= need:
        return "Enough"
    if value >= 1:
        return "Marginal"
    return "Likely to break"


def _strength_reason(sf: float, verdict: str, strongest: float | None = None) -> str:
    out = f"Strong enough for your load (safety {_format_safety(sf)})" if verdict == "fine" else f"{verdict.capitalize()} under your load (safety {_format_safety(sf)})"
    if strongest and strongest / max(sf, 1e-9) >= 1.2:
        out += f" — the strongest direction holds {strongest / sf:.1f}× more"
    elif strongest:
        out += " — the strongest direction for your load"
    return out


def _strength_story_parts(r: dict) -> dict:
    """Short, independently usable parts of the strength explanation."""
    z = r["critical_point_print"][2]
    why = {
        "layers": "There the layers get pulled or slid apart. The bond between layers is the weakest part of any print, so turning the part helps most.",
        "material": "There the plastic itself is overloaded, not the bond between layers. More walls, more infill or a thicker design help most.",
        "lines": ("There the printed lines lying side by side get pulled apart, like a screw tearing out through the walls."
                  " More walls around that spot, more material there, or turning the part so the lines run along the pull help most."),
        "buckling": format_message("strength.geometry.buckling_fix"),
    }[r["mode"]]
    # Strong enough: don't nudge people to change a part that already holds (the why stays in the full story).
    helps = "Nothing needs to change for this load." if r.get("verdict") == "fine" else why
    return {"weakest": f"The weakest spot is about {max(z, 0):.0f} mm above the bed.", "helps": helps, "why": why}


def _strength_story(mesh: Mesh, up, r: dict, p: Profile) -> str:
    """The result in plain words: will it hold, where it gives way, and why there."""
    sf, z = r["safety_factor"], r["critical_point_print"][2]
    if not np.isfinite(sf):
        return "Plenty of reserve. The load hardly stresses the part. It will hold."
    head = {
        "fine": "It should hold the load you set.",
        "borderline": "It might break under the load you set; real prints vary.",
        "likely to break": (format_message("strength.residual.likely_to_break") if r.get("residual_stress", {}).get("included")
                            else f"It will most likely break: it holds only about {sf:.0%} of the load you set."),
    }[r["verdict"]]
    if r["mode"] == "buckling":
        head = format_message(f"strength.geometry.buckling_{'holds' if r['verdict'] == 'fine' else 'risk'}")
    why = _strength_story_parts(r)["why"]
    near = (" It's right next to where the part is held: a real screw, clamp or glue spreads the load a bit,"
            " so it is probably somewhat stronger there than shown.") if r.get("near_support") else ""
    return f"{_strength_band(sf, required_safety(p))}. {head} The weakest spot is about {max(z, 0):.0f} mm above the bed (red arrow). {why}{near}{_use_story(p)}"


def _use_story(p: Profile) -> str:
    """What heat and a load that stays on do to the result, in plain words."""
    from ..service import CONSTANT_LOAD_SAFETY, heat_factor, heat_limit

    out = ""
    k = heat_factor(p)
    if k < 0.999:
        name, limit, t = p.material["name"], heat_limit(p), p.use_temp_c
        if t >= limit:
            out += (f" At {t:.0f} °C {name} is past the point where it goes soft (about {limit:.0f} °C): it keeps only about {k:.0%} of"
                    " its stiffness and strength and will sag or bend over time even under a small load. A material that takes more heat is the real fix.")
        else:
            out += f" At {t:.0f} °C {name} keeps only about {k:.0%} of its strength and stiffness (already included above)."
    creep = creep_estimate(p)
    if creep:
        out += (f" Your room-temperature creep test estimates {creep['factor']:.2f} times the initial sag after a year. "
                f"The strength check uses a ×{required_safety(p):.2f} target based on that estimate.")
        if creep["provisional"]:
            out += " This early estimate may change as you add readings through four weeks."
    elif p.constant_load:
        out += (f" The load stays on: the check asks for at least ×{CONSTANT_LOAD_SAFETY:.0f} because plastic slowly stretches"
                " under a steady load (creep). Warmth makes creep much worse.")
    return out


OPT_TARGET = {"strength": 3.0}  # safety the optimiser aims for, by goal (default 2.0)


OPT_WALLS = range(2, 11)


OPT_LAYERS = range(3, 13)


OPT_INFILL = (0.10, 0.15, 0.20, 0.25, 0.35, 0.50, 0.75, 1.0)


OPT_MAX_STEPS = 16


def optimise_strength(up, loads: list[dict], profile: dict | None = None, target: float | None = None, zones: bool = False,
                      this_orientation: bool = False, baseline: dict | None = None) -> dict:
    """The least plastic that reaches ``target`` safety (default ×2, ×3 for the strength goal):
    start from a light print and add, one step at a time, whatever buys the most safety per gram
    — walls, top/bottom layers or infill. Tried for this orientation and the strongest one.
    ``zones``: also try stronger zones only around the most stressed places (slicer modifier
    volumes), the rest of the part light; returned as ``zoned`` with the plastic it saves.
    ``this_orientation``: only this orientation and no "what else" ideas: the app's automatic
    settings for a load, which must not turn the part and should cost about one strength run.
    ``baseline`` (walls, infill, top, bottom; e.g. the goal's settings): its safety, grams and
    minutes come back as ``baseline`` so the app can say what the change costs or saves."""
    from ..strength import cases_safety, evaluate_cases, find_zones

    s = _S._require()
    p = Profile.from_dict(profile)
    cases = _load_case(loads)
    target = float(target or max(OPT_TARGET.get(p.goal, 2.0), required_safety(p)))
    up = (np.asarray(up, float) / np.linalg.norm(up)).tolist()
    axes = np.vstack([np.eye(3), -np.eye(3)])
    ups = [up]
    if not this_orientation:
        strongest = axes[int(np.argmax(cases_safety(cases, axes, p, _shells(p))))]
        ups += [strongest.tolist()] if abs(float(np.dot(strongest, up))) < 0.99 else []
    plans = []
    for i, u in enumerate(ups):
        with stage(i / len(ups), (i + 1) / len(ups)):
            plans.append(_climb(cases, u, p, target, s.mesh))
    for plan, u in zip(plans, ups):
        plan["up"] = u
        plan["orientation"] = "this one" if u is up else "strongest"
    # Reaching the target wins; then the lighter print; if neither does, the safer one.
    best = min(plans, key=lambda q: (not q["met"], q["grams"] if q["met"] else -q["safety_factor"]))
    best["alternatives"] = [q for q in plans if q is not best]
    best["target"] = target
    best["explanation"] = _optimise_story(best, target, len(plans) > 1)
    if baseline:
        from ..strength import Shells, evaluate_cases

        sh = Shells(walls=int(baseline["walls"]), infill=float(baseline["infill"]), top=int(baseline["top"]), bottom=int(baseline["bottom"]))
        grams, minutes = _grams_minutes_of(cases[0].model, best["up"], sh, p, s.mesh)
        best["baseline"] = {"safety_factor": float(evaluate_cases(cases, best["up"], p, sh)["safety_factor"]),
                            "grams": round(grams, 1), "minutes": round(minutes, 1)}
    if not best["met"] and not this_orientation:  # settings alone aren't enough: what else would do it
        from ..strength import Shells

        sh = Shells(walls=best["walls"], infill=best["infill"], top=best["top"], bottom=best["bottom"])
        result = _evaluate(cases, best["up"], p, sh)
        best["beyond"] = _preferred(_beyond_settings(result, p, profile, loads, cases, best["up"], sh, target), target)
    if zones:
        with stage(0.9, 1.0):
            # Where the zones go: where the user's own strength check found the stress, when it is for this
            # orientation, loads and material; else a quick solve of a light print shows it (T-290).
            mine = _stressed_for(s, cases[0].model, best["up"], p)
            if mine is not None:
                fi, ignore = mine
            else:
                light = _shells(p, walls=OPT_WALLS[0], infill=OPT_INFILL[0])
                r = evaluate_cases(cases, best["up"], p, light)
                fi, ignore = r["fi"], cases[[c.number for c in cases].index(r["case"])].ignore
            boxes = find_zones(cases[0].model, fi, ignore, best["up"], OPT_WALLS[0], OPT_INFILL[0])
            if boxes:
                zoned = _climb(cases, best["up"], p, target, s.mesh, boxes)
                zoned["up"] = best["up"]
                zoned["saves_grams"] = round(best["grams"] - zoned["grams"], 1)
                zoned["saves_min"] = round(_grams_minutes(zoned["saves_grams"], p), 1)
                zoned["zones_from"] = "strength result" if mine is not None else "light print"
                zoned["explanation"] = _zoned_story(zoned, best, target)
                best["zoned"] = zoned
    return best


def optimize(goal: str = "balanced", loads: list[dict] | None = None, depth: str = "extended", profile: dict | None = None,
             owned: list[str] | None = None, materials: list[str] | None = None, nozzles: list[str] | None = None,
             environment: dict | None = None, constraints: dict | None = None, change: str = "slicer",
             faces: dict | None = None, budget_s: float | None = None) -> dict:
    """The new UI's optimizer (optimizer.py): 2–4 verified options for ``goal`` (strong / balanced / fast /
    light / quality) over orientation, material, nozzle, layer height and walls / top / infill, plus local
    reinforcements at Complete depth (or whenever ``change="design"``).

    ``loads`` as for strength (none, or goal quality: no strength solve). ``depth``: fast / extended / complete.
    ``profile``: printer, nozzle, use_temp_c, constant_load and repeated_load (fillet advice only).
    Lock a material with ``constraints``. ``owned``: materials on the user's shelf. ``materials`` / ``nozzles``: the only ones
    to consider (nozzles = the ticked sizes). ``environment``: {"temp_c", "touches": [water, food, outdoors,
    abrasion, oil_fuel, solvent, skin]}. ``constraints`` ("Your choices", hard limits): material, nozzle,
    layer_height, walls, top, bottom, infill, up. ``change``: slicer / design / both. ``faces`` as in orientations
    (``{"clean": "visible"}``: the guessed faces people see, the Quality goal's default): orientations that put
    support on them rank lower. ``budget_s``: a time budget in seconds; when it runs out the search stops and picks from
    what it checked (``ended_early``, and "Continue the search" first among the ways out). While it runs, the best
    options so far go to progress.partial (the web worker posts them for "Stop & show best so far")."""
    from .. import optimizer

    s = _S._require()
    d = dict(profile or {})
    d.pop("goal", None)
    base = Profile.from_dict(d)
    env = dict(environment or {})
    env.setdefault("temp_c", base.use_temp_c)
    return optimizer.optimize(s.mesh, goal=goal, loads=loads or None, depth=depth, base=base, owned=owned, materials=materials,
                              nozzles=nozzles, environment=env, constraints=constraints, change=change,
                              load_case=_load_case, climb=_climb, grams_minutes=_grams_minutes_of,
                              evaluate=_evaluate, orientation_settings=_orient.orientation_settings,
                              zone_info=_zones._zone_info, unit_shape=_zones._unit_shape, mark_faces=_orient._pose_marks(faces), budget_s=budget_s)


def estimate_runtime(depth: str = "extended", loads: list[dict] | None = None, profile: dict | None = None,
                     goal: str = "balanced", owned: list[str] | None = None, materials: list[str] | None = None,
                     nozzles: list[str] | None = None, environment: dict | None = None,
                     constraints: dict | None = None, change: str = "slicer") -> dict:
    """Calibrated runtime estimate for ``optimize`` on the loaded part: how long ``depth`` (fast / extended /
    complete) takes here (``seconds``) and in the browser (``browser_seconds``, ~3× slower in Pyodide).
    Computed from triangle count, size, voxel count and candidate count without running FEA or importing scipy."""
    from .. import optimizer

    s = _S._require()
    d = dict(profile or {})
    d.pop("goal", None)
    base = Profile.from_dict(d)
    env = dict(environment or {})
    env.setdefault("temp_c", base.use_temp_c)
    return optimizer.estimate_runtime(s.mesh, depth=depth, loads=loads or None, base=base, goal=goal,
                                      owned=owned, materials=materials, nozzles=nozzles, environment=env,
                                      constraints=constraints, change=change, target_voxels=_S._grid("strength_voxels"))


def _stressed_for(s: _S.Session, model, up, p: Profile) -> tuple[np.ndarray, np.ndarray] | None:
    """The last strength check's stress (fi, ignore), if it was for this model (the same loads and grid),
    build direction and material; None otherwise."""
    st = s.stressed
    if (st is None or st["model"] is not model or float(np.dot(st["up"], np.asarray(up, float) / np.linalg.norm(up))) < 0.999
            or st["material"] != json.dumps(p.material, sort_keys=True)):
        return None
    return st["fi"], st["ignore"]


def _grams_minutes(grams: float, p: Profile) -> float:
    """About how long ``grams`` of plastic take to print (checks.print_minutes)."""
    from ..checks import print_minutes

    return print_minutes(grams / p.material["density_g_cm3"] * 1000, p)


def _zoned_story(zoned: dict, plain: dict, target: float) -> str:
    z = zoned["zones"][0]
    head = (f"Stronger only where it's needed: {len(zoned['zones'])} zone{'s' if len(zoned['zones']) > 1 else ''} with "
            f"{z['infill']:.0%} infill and {z['walls']} walls, the rest {zoned['walls']} walls and {zoned['infill']:.0%} infill")
    where = " The zones sit where your strength check found the stress." if zoned.get("zones_from") == "strength result" else ""
    if not zoned["met"]:
        return f"{head} only reach {_format_safety(zoned['safety_factor'])} (you need ×{target:.0f}).{where}"
    if zoned["saves_grams"] > 0.5:
        time = f" and about {zoned['saves_min']:.0f} min" if zoned.get("saves_min", 0) >= 1 else ""
        return (f"{head}: {_format_safety(zoned['safety_factor'])} with {zoned['grams']:.0f} g — {zoned['saves_grams']:.0f} g{time} less than the same settings"
                f" everywhere (checked again with the zones).{where}")
    return f"{head}: {_format_safety(zoned['safety_factor'])}, but it saves almost nothing here — the load spreads through most of the part.{where}"


def _grams_minutes_of(model, up, sh, p: Profile, mesh: Mesh, volume_mm3: float | None = None) -> tuple[float, float]:
    """Grams and print minutes of the plastic inside the part with shells ``sh`` (voxel volume scaled
    to ``volume_mm3`` or the mesh's true volume)."""
    from ..checks import shell_infill_minutes
    from ..strength import material_split

    vol = abs(mesh.volume()) if volume_mm3 is None else float(volume_mm3)
    scale = vol / (len(model.elems) * model.grid.h**3)
    shell, infill = (v * scale for v in material_split(model, up, sh, p))
    return (shell + infill) / 1000 * p.material["density_g_cm3"], shell_infill_minutes(shell, infill, p)


def _climb(cases, up, p: Profile, target: float, mesh: Mesh, zones: list | None = None,
           precond_cache: dict | None = None, eval_fn=None, solid_known_ok: bool = False) -> dict:
    """Greedy search from a light print: each step takes the change with the most safety per cost.
    Cost counts plastic and print time alike (each relative to the light start): walls take longer
    per gram than infill, so a gram of walls costs more. With ``zones`` the zones' own infill and
    walls are steps too."""
    from dataclasses import replace

    from ..strength import Shells, evaluate_cases

    model = cases[0].model
    pc = {} if precond_cache is None else precond_cache
    memo: dict = {}  # sh -> (sf, u_all, exact_bool)
    costs: dict = {}

    def solve_sh(sh, rtol: float = 1e-3, x0=None):
        exact = rtol <= 1e-4
        hit = memo.get(sh)
        if hit is not None and (hit[2] or not exact):
            return hit[0], hit[1]
        start = hit[1] if hit is not None else x0
        if eval_fn is not None:
            val, u_all = eval_fn(sh, rtol, start)
        else:
            r = evaluate_cases(cases, up, p, sh, x0=start, rtol=rtol, precond_cache=pc, geometry_checks=False)
            val, u_all = float(r["safety_factor"]), r.get("_u_all")
        memo[sh] = (val, u_all, exact)
        return val, u_all

    def sf(sh, rtol: float = 1e-3, x0=None):
        return solve_sh(sh, rtol=rtol, x0=x0)[0]

    def gm(sh):
        if sh not in costs:
            costs[sh] = _grams_minutes_of(model, up, sh, p, mesh)
        return costs[sh]

    def grams(sh):
        return gm(sh)[0]

    sh = Shells(walls=OPT_WALLS[0], infill=OPT_INFILL[0], top=OPT_LAYERS[0], bottom=OPT_LAYERS[0], zones=tuple(zones or ()))
    solid = Shells(walls=OPT_WALLS[-1], infill=OPT_INFILL[-1], top=OPT_LAYERS[-1], bottom=OPT_LAYERS[-1],
                   zones=tuple(replace(z, walls=OPT_WALLS[-1], infill=OPT_INFILL[-1]) for z in zones or ()))
    g0, m0 = (max(v, 1e-6) for v in gm(sh))

    def cost(q):
        g, m = gm(q)
        return g / g0 + m / m0

    # Check the light start first; if it already meets the target (or a prior scan already did),
    # skip solving the solid upper bound.
    cur_sf, cur_u = solve_sh(sh, rtol=1e-3)
    if cur_sf >= target:
        cur_sf, cur_u = solve_sh(sh, rtol=1e-4, x0=cur_u)
    if cur_sf < target and not solid_known_ok:
        solid_sf, _ = solve_sh(solid, rtol=1e-3, x0=cur_u)
        if solid_sf < target:  # not even a solid print is enough: no need to search
            sh = solid
            cur_sf, cur_u = solve_sh(sh, rtol=1e-4)
    steps = []
    for n in range(OPT_MAX_STEPS):
        progress.report(n / OPT_MAX_STEPS, "Trying settings")
        if cur_sf >= target:
            cur_sf, cur_u = solve_sh(sh, rtol=1e-4, x0=cur_u)
            if cur_sf >= target:
                break
        if sh == solid:
            break
        moves = []
        if sh.walls + 1 in OPT_WALLS:
            moves.append((replace(sh, walls=sh.walls + 1), f"{sh.walls + 1} walls"))
        if sh.top + 1 in OPT_LAYERS:
            moves.append((replace(sh, top=sh.top + 1, bottom=sh.bottom + 1), f"{sh.top + 1} top and bottom layers"))
        k = OPT_INFILL.index(sh.infill)
        if k + 1 < len(OPT_INFILL):
            moves.append((replace(sh, infill=OPT_INFILL[k + 1]), f"{OPT_INFILL[k + 1]:.0%} infill"))
        if sh.zones:  # all zones step together
            z = sh.zones[0]
            k = OPT_INFILL.index(z.infill)
            if k + 1 < len(OPT_INFILL):
                moves.append((replace(sh, zones=tuple(replace(q, infill=OPT_INFILL[k + 1]) for q in sh.zones)), f"{OPT_INFILL[k + 1]:.0%} infill in the zones"))
            if z.walls + 1 in OPT_WALLS:
                moves.append((replace(sh, zones=tuple(replace(q, walls=z.walls + 1) for q in sh.zones)), f"{z.walls + 1} walls in the zones"))
        gain = []
        for m, label in moves:
            msf = sf(m, rtol=1e-3, x0=cur_u)
            if np.isfinite(msf) and cur_sf > 0:
                score = np.log(msf / cur_sf) / max(cost(m) - cost(sh), 1e-3)
                if score > 1e-4:
                    gain.append((score, m, label))
        if not gain:
            break
        _, sh, label = max(gain, key=lambda g: g[0])
        cur_sf, cur_u = solve_sh(sh, rtol=1e-3, x0=cur_u)
        steps.append({"change": label, "safety_factor": float(cur_sf)})
    final_sf, _ = solve_sh(sh, rtol=1e-4, x0=cur_u)
    if eval_fn is None:
        final_sf = _evaluate(cases, up, p, sh)["safety_factor"]
    settings = {"wall_loops": str(sh.walls), "top_shell_layers": str(sh.top), "bottom_shell_layers": str(sh.bottom),
                "sparse_infill_density": f"{sh.infill:.0%}"}
    if p.goal == "material":  # lightning infill only holds up the top surface, not a load
        settings["sparse_infill_pattern"] = "gyroid"
    out = {"met": bool(final_sf >= target), "safety_factor": float(final_sf), "grams": round(grams(sh), 1), "minutes": round(gm(sh)[1], 1),
           "walls": sh.walls, "top": sh.top, "bottom": sh.bottom, "infill": sh.infill, "settings": settings, "steps": steps}
    if sh.zones:
        out["zones"] = [_zones._zone_info(replace(z, walls=max(z.walls, sh.walls), infill=max(z.infill, sh.infill)), mesh, up, p.printer["bed"], _orient._moving(p))
                        for z in sh.zones]
    return out


def _optimise_story(best: dict, target: float, compared: bool) -> str:
    where = " Printed in the strongest orientation." if best["orientation"] == "strongest" else ""
    setting = f"{best['walls']} walls, {best['top']} top / {best['bottom']} bottom layers and {best['infill']:.0%} infill"
    if best["met"]:
        if best["safety_factor"] > 100:
            out = f"{setting} are far above the ×{target:.0f} target with about {best['grams']:.0f} g of plastic.{where}"
        else:
            out = f"{setting} reach {_format_safety(best['safety_factor'])} (you need ×{target:.0f}) with about {best['grams']:.0f} g of plastic.{where}"
        alt = best["alternatives"]
        if compared and alt and alt[0]["met"]:
            out += f" The other orientation needs {alt[0]['grams']:.0f} g for the same."
        return out
    return (f"Even {setting} only reach {_format_safety(best['safety_factor'])} (you need ×{target:.0f}).{where} "
            "Settings alone can't make it strong enough: make the weak spot thicker, round its corners, use a stronger material, or let it carry less.")


def _resolve_loads(loads: list[dict]) -> list[dict]:
    """Expand any edge/inside-corner load (``{"edge": id, "edge_kind": "feature"|"concave", ...}``)
    into a standard ``strength.py`` ``points`` + ``radius`` band. Loads already using ``region``,
    ``point`` or ``points`` pass through untouched."""
    if not any("edge" in l and l.get("points") is None and l.get("point") is None for l in loads):
        return loads
    from ..regions import EDGE_BAND_RADIUS, edge_load, unpack_polylines

    feature_edges, concave_edges = _load._edges()
    groups = {"feature": feature_edges, "concave": concave_edges}
    unpacked: dict[str, list[np.ndarray]] = {}
    out = []
    for load in loads:
        if "edge" not in load or load.get("points") is not None or load.get("point") is not None:
            out.append(load)
            continue
        edge_kind = load.get("edge_kind", "feature")
        if edge_kind not in groups:
            raise ValueError(f"edge_kind must be 'feature' or 'concave', got {edge_kind!r}")
        if edge_kind not in unpacked:
            unpacked[edge_kind] = unpack_polylines(groups[edge_kind])
        idx = int(load["edge"])
        lines = unpacked[edge_kind]
        if not 0 <= idx < len(lines):
            raise ValueError(f"Unknown {edge_kind} edge {idx}")
        extra = {k: v for k, v in load.items() if k not in ("edge", "edge_kind", "radius", "kind")}
        radius = float(load.get("radius", EDGE_BAND_RADIUS))
        out.append(edge_load(lines[idx], kind=load["kind"], radius=radius, **extra))
    return out


def _load_case(loads: list[dict], voxels: int | None = None):
    """Prepared load cases (list, see strength.prepare_cases), cached per load set and grid size."""
    from ..regions import find_regions
    from ..strength import prepare_cases

    s = _S._require()
    voxels = voxels or _S._grid("strength_voxels")
    resolved = _resolve_loads(loads)
    key = json.dumps([resolved, voxels], sort_keys=True)
    if s.load_case is None or s.load_case[0] != key:
        if s.regions is None:
            s.regions = find_regions(s.mesh)
        s.load_case = (key, prepare_cases(s.mesh, s.regions, resolved, voxels))
    return s.load_case[1]


TOPOLOGY_VOXELS = {False: 3_000, True: 12_000}  # Quick (~1 min in the browser) / Fine (~3 min)


def topology(up, loads: list[dict], profile: dict | None = None, keep: float = 0.4, fine: bool = False) -> dict:
    """A suggested lighter shape (topopt.py): where the plastic carries ``loads`` when only ``keep`` (0.15–0.9)
    of the part's volume may stay. Only removes plastic; cubes at loads and held faces stay. ``fine``: a
    finer grid (12 000 cubes instead of 3 000, about 4× slower). Returns the blocky shape as ``stl`` and
    ``triangles`` (float32, part coordinates), its plastic against the part's, and the safety factor of
    both printed solid on the same grid (a fair comparison, not the real print's)."""
    from .. import topopt
    from ..progress import stage

    s = _S._require()
    p = Profile.from_dict(profile)
    cases = _load_case(loads, TOPOLOGY_VOXELS[bool(fine)])
    model = cases[0].model
    with stage(0.0, 0.9):
        r = topopt.optimise(cases, up, p, float(keep))
    solid = topopt.keep_connected(model, r["density"] > topopt.SOLID, r["loaded"], r["held"])
    if not solid.any():
        raise ValueError("No shape at this setting joins the load to where the part is held: keep more plastic")
    tris = topopt.voxel_surface(model.grid, model.elems[solid])
    with stage(0.9, 1.0):
        before = min(topopt.safety_solid(c, up, p, np.ones(len(solid), bool)) for c in cases)
        after = min(topopt.safety_solid(c, up, p, solid) for c in cases)
    grams = p.material["density_g_cm3"] / 1000
    part_g, shape_g = abs(s.mesh.volume()) * grams, abs(Mesh(tris).volume()) * grams
    need = required_safety(p)
    text = (f"A lighter shape that keeps the plastic where the load runs: about {shape_g:.0f} g instead of {part_g:.0f} g "
            f"({100 * shape_g / part_g:.0f} %). Printed solid it holds {_format_safety(after)} the load (the whole part solid: {_format_safety(before)}). ")
    text += ("That's still enough. " if after >= need else
             f"That's below ×{need:g}: keep more plastic, or thicken the thinnest struts when you redesign it. ")
    if r["keep_used"] > float(keep) + 0.01:
        text += (f"The held and loaded faces stay whole and already take much of the plastic, so it keeps "
                 f"{100 * r['keep_used']:.0f} % instead of {100 * float(keep):.0f} %. ")
    text += "It's blocky and not a finished part: use it as a guide to redesign the part in CAD."
    return {"keep": float(keep), "keep_used": r["keep_used"], "rounds": r["rounds"], "cubes": int(len(solid)), "part_g": round(part_g, 1),
            "shape_g": round(shape_g, 1), "safety_solid_part": before, "safety_solid_shape": after, "enough": after >= need,
            "explanation": text, "stl": to_binary_stl(Mesh(tris)), "triangles": tris.astype(np.float32).tobytes()}


def _evaluate(cases, up, p: Profile, sh, *, released: ReleasedStrain | None = None) -> dict:
    """``strength.evaluate_cases`` for the cached load cases, remembered once: "Will it hold?", the
    knock check, the stronger options and the material advisor solve the same thing one after another."""
    from ..strength import evaluate_cases

    s = _S._require()
    ours = s.load_case is not None and s.load_case[1] is cases
    key = (s.load_case[0] if ours else None, tuple(np.round(np.asarray(up, float), 9)), p, sh, released)
    if ours and s.solved is not None and s.solved[0] == key:
        return dict(s.solved[1])
    r = evaluate_cases(cases, up, p, sh, released=released)
    if ours:
        s.solved = (key, r)
    return dict(r)


def _shells(p: Profile, walls: int | None = None, infill: float | None = None, top: int | None = None, bottom: int | None = None,
            zones: list[dict] | None = None):
    """How the part will be printed; anything not given comes from the goal's usual settings.
    ``zones``: stronger zones (lo, hi, walls, infill, as optimise_strength returns them) and the
    advanced design boxes (``kind``, see volume_at): hidden gaps count, supports don't change the part."""
    from ..strength import Shells

    plan = goal_plan(p)
    return Shells(walls=int(plan["walls"] if walls is None else walls), infill=float(plan["infill"] / 100 if infill is None else infill),
                  top=int(plan["top"] if top is None else top), bottom=int(plan["bottom"] if bottom is None else bottom),
                  zones=tuple(_zones._box_zone(z) for z in zones or () if z.get("kind", "stronger") in ("stronger", "void")))


def _printed(model, *, cache: bool = True):
    """What the loaded G-code puts in each cube of ``model`` (cached per analysis grid)."""
    from ..gcode import deposit
    from ..strength import printed_from
    from ..voxel import inside_fraction

    s = _S._require()
    g = s.gcode
    if id(model) not in g["printed"]:
        q, shift, rot = g["q"], g["shift"], g["rot"]

        def to_part(x, is_direction):  # printer -> part: part = rotᵀ (q x + shift)
            return (x @ q.T) @ rot if is_direction else (x @ q.T + shift) @ rot

        dep = deposit(g["toolpath"], model.grid, model.elems, to_part)
        pr = printed_from(dep, model, inside_fraction(s.mesh, model.grid))
        result = pr, dep["missed"]
        if cache:
            g["printed"] = {id(model): result}
        return result
    return g["printed"][id(model)]


def _strength_on_grid(cases, up, p, sh, detailed, h, *, trial: bool = False) -> dict:
    """Recompute print deposition and bonding for the grid used in this solve."""
    from dataclasses import replace

    from ..strength import evaluate_cases, material_for

    w = _warp._matching_warp(_S._require(), up, p, sh)
    released = w.get("released") if w and (not detailed or w.get("from_gcode")) else None
    if not detailed:
        return _evaluate(cases, up, p, sh, released=released)
    model = cases[0].model
    printed, missed = _printed(model, cache=not trial)
    if h is not None:
        usual = evaluate_cases(cases, up, p, sh, printed, released=released)["safety_factor"]
        bond, source = _heat_mod._bond_per_cube(model, _S._require().gcode, h, material_for(p))
        printed = replace(printed, bond=bond)
    r = evaluate_cases(cases, up, p, sh, printed, released=released)
    r["detailed"] = {"missed_pct": round(missed * 100, 1), "fill_pct": round(float(printed.fill.mean()) * 100, 1)}
    if h is not None:
        r["heat"] = _heat_strength(model, h, bond, source, r, usual)
    return r


def _thin_feature(model, critical_point) -> str | None:
    from ..strength import THIN_GRID_CELLS, thin_grid_cells

    thin = thin_grid_cells(model)
    at = np.floor((critical_point - model.grid.origin) / model.grid.h).astype(int)
    if (thin & (model.elems == at).all(axis=1)).any():
        return "weak_spot"
    if any(f["type"] == "rounded_edge" and f["tag"] != "none" and f["radius"] <= THIN_GRID_CELLS * model.grid.h
           for f in _load._features()):
        return "fillet"
    return "wall" if thin.any() else None


def _strength_grid_check(cases, r, loads, up, p, sh, detailed, h):
    """Try one bounded refinement. The original result and cache survive a failed trial."""
    from ..strength import finer_grid, prepare_cases

    model = cases[0].model
    reason = _thin_feature(model, r["critical_point"])
    if reason is None:
        return cases, r, None
    s = _S._require()
    before = r["safety_factor"]
    check = {"status": "limited", "coarse_voxel_mm": model.grid.h, "coarse_safety_factor": before,
             "reason": {"key": f"strength.grid.{reason}", "params": {}}}
    try:
        grid = finer_grid(s.mesh, model, _S._grid("strength_voxels"))
        if grid is not None:
            check["status"] = "failed"
            refined = prepare_cases(s.mesh, s.regions, _resolve_loads(loads), grid=grid)
            trial = _strength_on_grid(refined, up, p, sh, detailed, h, trial=True)
            # A finer grid can push an optional solve over its budget. Keep a successful
            # geometry check when the finer grid still calls for that check but can't finish it.
            geometry_lost = any(
                check.get("status") == "solved" and new.get("geometry_checks", {}).get(kind, {}).get("status") == "unavailable"
                for old, new in zip(r["cases"], trial["cases"])
                for kind, check in old.get("geometry_checks", {}).items()
            )
            if not geometry_lost and trial["_cases_converged"] and np.isfinite(trial["fi"]).all() and np.isfinite(trial["u"]).all():
                after = trial["safety_factor"]
                moved = not np.isclose(after, before, rtol=0.05, atol=0) or trial["verdict"] != r["verdict"]
                check.update(status="changed" if moved else "similar", fine_voxel_mm=grid.h)
                cases, r = refined, trial
    except (ValueError, RuntimeError, ArithmeticError, MemoryError, np.linalg.LinAlgError):
        check["status"] = "failed"
    status, params = check["status"], {}
    if status == "changed":
        after = r["safety_factor"]
        if np.isfinite([before, after]).all():
            old, new = _format_safety(before), _format_safety(after)
            if old == new:
                old, new = f"about ×{before:.2f}", f"about ×{after:.2f}"
            if old != new:
                status = "changed_safety"
                params = {"before": old, "after": new}
    check["message"] = {"key": f"strength.grid.{status}", "params": params}
    return cases, r, check


def strength(up, loads: list[dict], profile: dict | None = None, walls: int | None = None, infill: float | None = None,
             top: int | None = None, bottom: int | None = None, fine: bool = False, detailed: bool = False,
             zones: list[dict] | None = None, heat: bool | None = None) -> dict:
    """Will it hold? Safety factor, failure mode and a per-face heat map (uint8, 255 = breaks).
    ``walls`` (lines), ``infill`` (0–1), ``top``/``bottom`` (solid layers) say how it will be printed
    (default: the goal's usual settings). With several load cases (``case`` on forces) the weakest
    decides; ``cases`` lists them all. ``fine`` uses a finer grid (slower): if the answer barely
    changes, the coarse one can be trusted (the usual mesh-convergence check in FEA). ``detailed``
    uses the G-code from ``load_gcode`` instead of walls/infill/top/bottom: the plastic really in
    each spot and which way its lines run (lines pulled apart sideways become a third way to break).
    ``zones``: stronger zones from optimise_strength (slicer modifier volumes). With ``detailed``,
    use a matching heat result if available. ``heat=True`` runs the heat model if needed and
    ``heat=False`` uses the usual layer strength. The ``heat`` result compares safety with usual
    bonding and locates the cold joint that sets the local strength. A thin feature on the normal grid
    can trigger one bounded finer solve, described in ``grid_check``. Large movement or compression
    in a thin wall can trigger further analysis, reported in ``geometry_checks``. Quick runs include a safety
    ``range`` from rechecking the same stresses with weaker and stronger layer bonds. A matching
    ``warping`` run supplies the stress locked in while printing (see ``residual_stress``)."""
    from ..strength import face_values, refine, safety_range, sample_nodes, surface_values

    s = _S._require()
    p = Profile.from_dict(profile)
    cases = _load_case(loads, _S._grid("fine_voxels") if fine else None)
    sh = _shells(p, walls, infill, top, bottom, zones)
    h = None
    if detailed:
        if s.gcode is None:
            raise ValueError("Load the sliced G-code first (detailed check)")
        if np.dot(s.gcode["up"], np.asarray(up, float) / np.linalg.norm(up)) < 0.999:
            raise ValueError("The G-code was sliced for another orientation: slice this one, or switch back")
        cached = s.gcode.get("heat")
        if heat is not False and cached is not None and cached[0][0] == _heat_mod._heat_key(p):
            h = cached[1][0]
        elif heat:
            h, _ = _heat_mod._heat(p)
    elif heat:
        raise ValueError("The heat option needs the detailed check (load the sliced G-code)")
    r = _strength_on_grid(cases, up, p, sh, detailed, h)
    # The optimiser can reuse these stresses on its usual grid even if the display uses a finer one.
    base_case = next(c for c in cases if c.number == r["case"])
    base_fi = r["fi"]
    if not fine:
        cases, r, grid_check = _strength_grid_check(cases, r, loads, up, p, sh, detailed, h)
        if grid_check is not None:
            r["grid_check"] = grid_check
    r["fine"] = bool(fine)
    r.pop("_cases_converged")
    model = cases[0].model
    if not (fine or detailed):
        bounds = safety_range(r["case_fi"], p, r["safety_factor"])
        if bounds is not None:
            r["range"] = bounds
    fi, vm, u = r.pop("fi"), r.pop("vm"), r.pop("u")
    heat = face_values(s.mesh, model.grid, model, fi)
    r["face_heat"] = (np.clip(heat / 1.25, 0, 1) * 255).astype(np.uint8).tobytes()
    # FEA view: a finer copy of the surface with smooth fields at every corner — % of strength used,
    # von Mises stress, displacement.
    fine = refine(s.mesh.triangles, model.grid.h)
    corners = fine.reshape(-1, 3)
    r["field_triangles"] = fine.astype(np.float32).tobytes()
    r["vertex_fi"] = (surface_values(model, fi, fine) * 100).astype(np.float32).tobytes()
    r["vertex_vm"] = surface_values(model, vm, fine).astype(np.float32).tobytes()
    r["vertex_disp"] = sample_nodes(model, u, corners).astype(np.float32).tobytes()
    # The analysis cubes themselves, for the section view (peaks are often inside the part).
    r["elements"] = {"centers": model.grid.centers(model.elems).astype(np.float32).tobytes(), "size_mm": float(model.grid.h),
                     "fi": (fi * 100).astype(np.float32).tobytes(), "vm": vm.astype(np.float32).tobytes()}
    worst = cases[[c.number for c in cases].index(r["case"])]
    ok = ~worst.ignore  # stresses right at the supports and loads are not meaningful (they grow as the grid gets finer)
    r["fi_max_pct"] = float(fi[ok].max(initial=0) * 100)
    r["required_safety"] = float(required_safety(p))
    r["creep"] = creep_estimate(p)
    r["vm_max_mpa"] = float(vm[ok].max(initial=0))
    r["mesh"] = {"voxels": int(len(model.elems)), "voxel_mm": float(model.grid.h), "nodes": int(model.n_nodes),
                 "load_n": float(np.linalg.norm(worst.forces.sum(axis=0)))}
    rot = print_rotation(s.mesh, up, p.printer["bed"], _orient._moving(p))[0]
    r["critical_point_print"] = _design._to_print_frame(s.mesh, rot, r["critical_point"])
    # Where to point the "weakest spot" arrow: the surface nearest to it (the spot itself is often inside).
    # Not a face on the bed (an arrow from below the bed can't be seen): the nearest face you can see.
    normals = np.cross(fine[:, 1] - fine[:, 0], fine[:, 2] - fine[:, 0])
    normals /= np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1e-12)
    dist = np.linalg.norm(fine.mean(axis=1) - r["critical_point"], axis=1)
    dist[normals @ rot[2] < -0.7] += 1e6
    tri = fine[int(np.argmin(dist))]
    n = np.cross(tri[1] - tri[0], tri[2] - tri[0])
    r["weak_marker"] = {"point": _design._to_print_frame(s.mesh, rot, tri.mean(axis=0)), "normal": (rot @ (n / max(np.linalg.norm(n), 1e-12))).tolist()}
    r.update(walls=sh.walls, infill=sh.infill, top=sh.top, bottom=sh.bottom, material=p.material["name"])
    r["stress_zones"] = _stress_zones(model, fi, worst.ignore, up, sh, s.mesh, p)
    s.weak_spot = (r["critical_point"], float(model.grid.h))  # design() suggests a chamfer there
    s.hot_spots = _hot_spots(model, fi, ok)  # ... and at the other inside corners that are nearly as weak
    s.strength_verdict = r["verdict"]
    s.stressed = None if detailed else {"model": base_case.model, "up": np.asarray(up, float) / np.linalg.norm(up),
                                       "material": json.dumps(p.material, sort_keys=True), "fi": base_fi, "ignore": base_case.ignore}
    residual = dict(r.get("residual_stress", {"included": False}))
    r["residual_stress"] = residual
    r["story_parts"] = _strength_story_parts(r)
    r["explanation"] = _strength_story(s.mesh, up, r, p)
    if residual["included"]:
        before, after = residual["safety_factor_without"], r["safety_factor"]
        params = {}
        status = "included"
        if before is not None and np.isfinite(after):
            old, new = (round(float(f"{value:.2g}"), 2) for value in (before, after))
            if old != new:
                status = "lower" if after < before else "higher"
                params = {"before": old, "after": new}
        residual["message"] = {"key": f"strength.residual.{status}", "params": params}
        residual["detail"] = format_message(residual["message"])
        r["explanation"] += f" {residual['detail']}"
    for check in r.get("geometry_checks", {}).values():
        if "detail" in check:
            r["explanation"] += f" {check['detail']}"
    if "grid_check" in r:
        check = r["grid_check"]
        r["explanation"] += f" {format_message(check['reason'])} {format_message(check['message'])}"
    return r


def _heat_strength(model, h, bond: np.ndarray, source: np.ndarray, r: dict, usual: float) -> dict:
    """What the print's cooling does to the safety, in plain words: this print against the same one with
    every layer bonded as usual, and how well the layers fused at the weakest spot."""
    sf = r["safety_factor"]
    at = int(np.argmin(np.linalg.norm(model.grid.centers(model.elems) - r["critical_point"], axis=1)))
    params = {"safety": round(sf, 1), "usual": round(usual, 1)}
    if sf < usual * 0.97:
        key = "strength.heat.weakened"
    elif sf > usual * 1.03:
        key = "strength.heat.stronger"
    else:
        key = "strength.heat.similar"
    message = {"key": key, "params": params}
    result = {"safety_factor": sf, "usual_safety_factor": usual, "bond_at_weakest": round(float(bond[at]), 2),
              "bond_min": round(float(bond.min()), 2), "message": message, "text": format_message(message)}
    weak = at if bond[at] < 1 else int(bond.argmin())
    if bond[weak] < 1:
        j = source[weak]
        p = np.array([*h.xy[j], h.joint_z[j]])
        point = _heat_mod._gcode_to_oriented(_S._require().gcode["rot"])(p)
        params = {"layer": int(h.layer[j]) + 1, "height": round(float(point[2]), 2),
                  "share": round(float(bond[weak]) * 100, 1), "warm_s": round(float(h.above_tg_s[j]), 2)}
        joint_message = {"key": "strength.heat.cold_joint", "params": params}
        detail = format_message(joint_message)
        result["weak_joint"] = {"point": point.tolist(), "layer": params["layer"], "strength_pct": params["share"],
                                "message": joint_message, "detail": detail}
        result["text"] += " " + detail
    return result


def strength_variant(up, loads: list[dict], data: bytes, profile: dict | None = None, walls: int | None = None,
                     infill: float | None = None, top: int | None = None, bottom: int | None = None,
                     zones: list[dict] | None = None) -> dict:
    """The strength of a changed copy of the loaded part (a design variant, as STL / 3MF ``data``) without
    replacing the part: for trying design changes in the background and showing only the one the user
    picks. Held faces and loads move to the variant's matching face (facing the same way, middle nearest
    the old one's, as the web app does for a new version; spots keep their point). Returns the safety
    factor, its verdict and how much plastic the change adds (``added_g``, solid)."""
    from ..regions import find_regions, region_info

    s = _S._require()
    p = Profile.from_dict(profile)
    if s.regions is None:
        s.regions = find_regions(s.mesh)
    old = region_info(s.mesh, s.regions)
    mesh = read_bodies(bytes(data))[0][1]
    variant = _S.Session(mesh, f"{s.name} (variant)", find_regions(mesh))
    new = region_info(mesh, variant.regions)
    moved = []
    for load in _resolve_loads(loads):
        if "region" not in load or load.get("point") is not None or load.get("points"):
            moved.append(load)
            continue
        was = old[load["region"]]
        same = [r for r in new if np.dot(r["normal"], was["normal"]) > 0.9]
        if not same:
            raise ValueError("A held or loaded face is gone in this design change")
        near = min(same, key=lambda r: np.linalg.norm(np.subtract(r["centroid"], was["centroid"])))
        moved.append({**load, "region": near["id"]})
    _S._session = variant
    try:
        r = strength(up, moved, profile, walls, infill, top, bottom, zones=zones)
    finally:
        _S._session = s
    added = (abs(mesh.volume()) - abs(s.mesh.volume())) / 1000 * p.material["density_g_cm3"]
    return {"safety_factor": r["safety_factor"], "verdict": r["verdict"], "added_g": float(added)}


def _stress_zones(model, fi: np.ndarray, ignore: np.ndarray, up, sh, mesh: Mesh, p: Profile) -> list[dict]:
    """"Stronger in one area" zones where this result's stress is high, instead of the user's guess:
    the optimiser's boxes round the most stressed cubes, printed like a picked zone (two more walls,
    at least 40 % infill) and never weaker than the rest of the part."""
    from ..strength import find_zones

    boxes = find_zones(model, fi, ignore, up, sh.walls + 2, max(0.4, sh.infill))
    return [{**_zones._zone_info(z, mesh, up, p.printer["bed"], _orient._moving(p)), "share_pct": round(share * 100)}
            for z, share in zip(boxes, _zone_shares(model, fi, ignore, up, boxes))]


def _zone_shares(model, fi: np.ndarray, ignore: np.ndarray, up, boxes: list) -> list[float]:
    """How hard the plastic in each zone works at its peak, as a share of the worst spot: tells the
    user which zone matters most."""
    worst = fi[~ignore].max(initial=0)
    if worst <= 0:
        return [0.0] * len(boxes)
    c = model.grid.centers(model.elems)
    return [float(fi[(z.gap(c @ rotation_to_z(up).T) <= 1e-9) & ~ignore].max(initial=0) / worst) for z in boxes]


def _hot_spots(model, fi: np.ndarray, ok: np.ndarray, most: int = 1500) -> tuple | None:
    """Centres of the analysis cubes with at least features.HOT_SHARE of the highest stress (not right at
    a support or load) and their share of it, at most ``most`` of the highest."""
    from ..features import HOT_SHARE

    top = fi[ok].max(initial=0)
    if top <= 0:
        return None
    idx = np.flatnonzero(ok & (fi >= HOT_SHARE * top))
    idx = idx[np.argsort(-fi[idx])[:most]]
    return model.grid.centers(model.elems[idx]), fi[idx] / top


def dynamics(up, loads: list[dict], profile: dict | None = None, drop_mm: float = 0.0, mounted_kg: float = 0.0, shake: bool = True,
             walls: int | None = None, infill: float | None = None, top: int | None = None, bottom: int | None = None,
             zones: list[dict] | None = None) -> dict:
    """Simplified dynamics: the lowest natural frequency as held (shakes along at that rate: a motor,
    fan or printer running near it makes it resonate) and its swing shape for the FEA view (``shake``);
    and the load falling ``drop_mm`` onto the part (or the part being knocked): the impact factor and
    the safety factor that leaves. ``mounted_kg``: something fixed on the loaded face that swings with
    it. Printed like the strength check (``walls`` … ``zones``), whose static solve it reuses."""
    from ..strength import impact_factor, refine, sample_nodes, vibration

    s = _S._require()
    p = Profile.from_dict(profile)
    cases = _load_case(loads)
    sh = _shells(p, walls, infill, top, bottom, zones)
    model = cases[0].model
    out = {"drop_mm": drop_mm, "frequency_hz": None, "sag_mm": None, "impact_factor": None, "static_safety": None, "impact_safety": None,
           "impact_factor_soft": None, "impact_safety_soft": None}
    if shake:
        hz, shape = vibration(cases[0], up, p, sh, mounted_kg)
        fine = refine(s.mesh.triangles, model.grid.h)
        disp = sample_nodes(model, shape, fine.reshape(-1, 3))
        out.update(frequency_hz=round(hz, 1), field_triangles=fine.astype(np.float32).tobytes(), vertex_disp=disp.astype(np.float32).tobytes(),
                   vertex_value=np.linalg.norm(disp, axis=1).astype(np.float32).tobytes())
    if drop_mm > 0:
        r = _evaluate(cases, up, p, sh)
        worst = cases[[c.number for c in cases].index(r["case"])]
        loaded = np.linalg.norm(worst.forces, axis=1) > 0
        sag = float(np.abs(np.einsum("ij,ij->i", r["u"][loaded], worst.forces[loaded] / np.linalg.norm(worst.forces[loaded], axis=1, keepdims=True))).mean())
        # The mount and the falling thing give too (springs in series with the part): a range from a hard
        # thing on a part screwed into wood to something soft, the stiffnesses from thresholds.yaml "drop".
        D = load_data("thresholds")["drop"]
        weight_n = float(np.linalg.norm(worst.forces[loaded], axis=1).sum())
        hard, soft = (impact_factor(drop_mm, sag + weight_n / D[k]) for k in ("hard_n_per_mm", "soft_n_per_mm"))
        sf = r["safety_factor"] if np.isfinite(r["safety_factor"]) else None
        out.update(sag_mm=round(sag, 3), impact_factor=round(hard, 2), impact_factor_soft=round(soft, 2), static_safety=r["safety_factor"],
                   impact_safety=None if sf is None else sf / hard, impact_safety_soft=None if sf is None else sf / soft)
    out["explanation"] = _dynamics_story(out)
    return out


def _dynamics_story(r: dict) -> str:
    hz, out = r["frequency_hz"], ""
    if hz is not None:
        where = ("very low: it sways visibly and anything shaking nearby can rock it" if hz < 20 else
                 "in the range of motors, fans and printers (20–200 Hz): if something near it runs at that rate it can resonate and rattle loose"
                 if hz < 200 else "high: everyday shaking won't make it resonate")
        out = f"It vibrates naturally at about {hz:.0f} Hz — {where}. Shown: how it swings (exaggerated)."
    if r["drop_mm"] > 0 and r["impact_safety"] is not None:
        hard, soft = r["impact_safety"], r["impact_safety_soft"]
        verdict = (": still fine." if hard >= 2 else ": it will likely break." if soft < 1
                   else ": a soft thing is fine, a hard one may break it." if soft >= 2 else ": it may break when that happens.")
        out += (f"{' ' if out else ''}If the load drops {r['drop_mm']:.0f} mm onto it, it hits about {r['impact_factor_soft']:.0f}–{r['impact_factor']:.0f}×"
                f" as hard as when held still, depending on how hard the falling thing and the mount are (assumed: from something"
                f" soft or rubber-footed to a hard object on a part screwed into wood; the part itself gives {r['sag_mm']:.2f} mm)."
                f" The safety drops from {_format_safety(r['static_safety'])} to between {_format_safety(hard)} and {_format_safety(soft)}" + verdict)
    return out


STRONGER_TURN = 1.3  # another build direction is offered when it holds at least this much more


def stronger_orientation(up, loads: list[dict], profile: dict | None = None, walls: int | None = None,
                         infill: float | None = None, top: int | None = None, bottom: int | None = None) -> dict:
    """The strongest of the six square build directions for ``loads`` (T-368: offered straight away when the
    strength check fails): its ``up`` and ``safety_factor``, and ``current`` for ``up`` on the same quick
    solve, so the two compare like with like. ``up`` is None when no direction holds STRONGER_TURN × more."""
    from ..strength import cases_safety

    p = Profile.from_dict(profile)
    case = _load_case(loads)
    now = np.asarray(up, float) / np.linalg.norm(up)
    axes = np.vstack([now, np.eye(3), -np.eye(3)])
    sfs = cases_safety(case, axes, p, _shells(p, walls, infill, top, bottom))
    best = int(np.argmax(sfs[1:])) + 1
    better = sfs[best] >= STRONGER_TURN * sfs[0] and abs(float(axes[best] @ now)) < 0.99
    return {"up": axes[best].tolist() if better else None, "safety_factor": float(sfs[best]), "current": float(sfs[0])}


def strength_options(up, loads: list[dict], profile: dict | None = None, walls: int | None = None, infill: float | None = None,
                     top: int | None = None, bottom: int | None = None) -> list[dict]:
    """Every way to make it stronger, each with its safety factor, what it costs and why it helps:
    slicer settings (more walls, infill, top/bottom layers), the strongest orientation, a design change
    at the weak spot (from the last strength check), other materials and a lighter load. In the order of
    ``_preferred`` (easiest that is enough first, materials and a lighter load last). ``base_safety`` on each: today's safety with the same shell model,
    so the +/- % compares like with like (the G-code strength check gives its own, different number)."""
    from dataclasses import replace

    from ..strength import cases_safety, material_volume, verdict
    from ..strength import evaluate_cases as evaluate_strength

    s = _S._require()
    p = Profile.from_dict(profile)
    case = _load_case(loads)
    sh = _shells(p, walls, infill, top, bottom)
    model = case[0].model
    to_grams = abs(s.mesh.volume()) / (len(model.elems) * model.grid.h**3) / 1000 * p.material["density_g_cm3"]
    base = _evaluate(case, up, p, sh)
    grams = material_volume(model, up, sh, p) * to_grams
    out = []

    def add(change, kind, sf, why, cost="", **extra):
        out.append({"change": change, "kind": kind, "safety_factor": None if sf is None else float(sf),
                    "verdict": None if sf is None else verdict(sf, required_safety(p)), "why": why, "cost": cost, **extra})

    def slicer(change, shells, why, settings):
        sf = evaluate_strength(case, up, p, shells)["safety_factor"]
        extra = material_volume(model, up, shells, p) * to_grams - grams
        add(change, "slicer", sf, why, f"+{extra:.0f} g plastic, a bit longer to print", settings=settings)

    slicer(f"{sh.walls + 2} walls instead of {sh.walls}", replace(sh, walls=sh.walls + 2),
           "Walls are the solid outside of the print and carry most of the load.", {"wall_loops": str(sh.walls + 2)})
    dense = max(0.4, sh.infill)
    if dense > sh.infill:
        slicer(f"{dense:.0%} infill instead of {sh.infill:.0%}", replace(sh, infill=dense),
               "Denser infill backs up the walls inside the part.", {"sparse_infill_density": f"{dense:.0%}"})
    more = replace(sh, top=sh.top + 3, bottom=sh.bottom + 3)
    slicer(f"{more.top} top and {more.bottom} bottom layers instead of {sh.top} and {sh.bottom}", more,
           "Solid top and bottom skins carry bending, like the flanges of a beam.",
           {"top_shell_layers": str(more.top), "bottom_shell_layers": str(more.bottom)})

    axes = np.vstack([np.eye(3), -np.eye(3)])
    sfs = cases_safety(case, axes, p, sh)
    best = int(np.argmax(sfs))
    if abs(float(np.dot(axes[best], np.asarray(up, float) / np.linalg.norm(up)))) < 0.99:
        add("Print in the strongest orientation", "orientation", sfs[best],
            "Turns the part so the load runs along the layers instead of pulling them apart.",
            "may need more support or take longer", up=axes[best].tolist())

    out += _beyond_settings(base, p, profile, loads, case, up, sh)
    for o in out:
        o["base_safety"] = float(base["safety_factor"])
    return _preferred(out, required_safety(p))


# How much fuss each kind of change is: the easiest one that is enough comes first (T-366).
EFFORT = {"slicer": 0, "orientation": 1, "design": 2, "split": 3}  # split: two pieces to print and glue


def _preferred(options: list[dict], need: float) -> list[dict]:
    """The easiest changes that reach ``need`` first (slicer settings before turning the part before a
    design change), then the rest strongest first. Another material comes after all of those, even
    when it's stronger: the user would have to buy and dry a new spool (T-366). Carrying less is
    always last: it doesn't make the part stronger, it's the way out when nothing else is enough."""
    def key(o):
        sf = o["safety_factor"]
        enough = sf is not None and sf >= need
        return (o["kind"] == "load", o["kind"] == "material", not enough,
                EFFORT.get(o["kind"], 3) if enough else 0, -(sf or 0))
    return sorted(options, key=key)


def _beyond_settings(base: dict, p: Profile, profile: dict | None, loads: list[dict], cases, up, shells,
                     need: float | None = None) -> list[dict]:
    """What helps besides slicer settings and orientation: a design change at the weak spot, another
    material, or a lighter load (that still reaches ``need``, default the usual safety). Also the way
    out when even the strongest settings aren't enough."""
    from ..features import concave_edges, inside_corner_load_advice
    from ..strength import verdict

    s = _S._require()
    override = need
    need = need or required_safety(p)
    out = []

    def add(change, kind, sf, why, cost="", *, target=need, **extra):
        out.append({"change": change, "kind": kind, "safety_factor": None if sf is None else float(sf),
                    "verdict": None if sf is None else verdict(sf, target), "why": why, "cost": cost, **extra})

    # Design: a chamfer at the inside corner next to the weak spot (see features._stress_chamfers).
    if s.weak_spot is not None:
        spot = np.asarray(s.weak_spot[0])
        edges = concave_edges(s.mesh)
        if edges and min(np.linalg.norm((e["a"] + e["b"]) / 2 - spot) for e in edges) <= 8 * s.weak_spot[1]:
            add("Round off the weak inside corners (design change)", "design", None,
                "The strength check found a sharp inside corner near the weak spot. "
                f"{inside_corner_load_advice(p.repeated_load)} "
                "Preview the round or self-supporting 45° chamfer as a design fix under Problems, then check strength again.",
                "changes the shape a little", edit="stress-chamfer")
    if s.weak_spot is not None:
        add("Make the weak spot thicker (design change)", "design", None,
            ("The plastic itself is the limit there: more cross-section is the direct fix. " if base["mode"] == "material" else "")
            + "Adds 1.5 mm of plastic all round the weak spot as an even sleeve with 45° ends (you can adjust the size under Problems). "
            "Test it to see how much it helps.", "changes the shape", edit="thicken-weak")

    # Other materials: stresses don't depend on stiffness here, only the strengths change. Fibre filled
    # ones (PLA-CF, PA-CF…) are never "stronger": the fibres make it stiffer, but more brittle and weaker
    # between the layers than the plain plastic (rules.yaml fibre_filled).
    from ..profile import fibre

    for mid, m in presets()["materials"].items():
        if mid == p.material_id or mid.startswith("tpu") or m.get("based_on", "").startswith("tpu") or fibre(m):
            continue
        other = Profile.from_dict({**(profile or {}), "material": mid})
        sf = _scaled_safety(base, p, other)
        if sf <= base["safety_factor"] * 1.15:  # only clearly stronger ones
            continue
        needs = []
        if m["warp_risk"] == "high" and p.enclosure == "none":
            needs.append("an enclosed printer (warps on an open one)")
        if m["abrasive"] and not p.printer.get("hardened_nozzle"):
            needs.append("a hardened nozzle")
        if m["hygroscopic"] == "high":
            needs.append("dry filament")
        add(f"Print it in {m['name']}", "material", sf,
            f"{m['name']} is stronger{' and its layers bond better' if other.z_strength_factor > p.z_strength_factor else ''}.",
            "needs " + ", ".join(needs) if needs else "", material=mid, target=override or required_safety(other))

    # Two pieces, each printed its strong way (T-347): only where no single orientation suits every part of it.
    from ..split import strength_cut

    cut = strength_cut(s.mesh, _load_case(loads), p, _shells(p)) if loads else None
    if cut is not None:
        add("Print it in two pieces, each its strong way", "split", base["safety_factor"] * cut["safety"] / cut["whole_safety"],
            "No single way to lay it suits every part of it: one part's layers always run across its load. Cut in two, each "
            "piece lies so its layers run along its own load, and the glued face sits where little load crosses it. Rough "
            "(from one solve); check the strength of the pieces after printing.", "two pieces to glue, with pins", purpose="strength")

    # Less load: the linear estimates scale directly. Recheck the load if the part still bends far.
    sf = base["safety_factor"]
    forces = [float(l.get("kg", 1)) for l in loads if l.get("kind") == "force"]
    if forces and np.isfinite(sf) and sf < need:
        share = _lighter_load_share(base, cases, up, p, shells, need)
        kg = f"about {forces[0] * share:.1f} kg instead of {forces[0]:g} kg" if len(forces) == 1 else f"about {share:.0%} of every load you set"
        add(f"Carry less: {kg}", "load", need,
            "The part holds a smaller load with the same margin. Spread the weight over two parts, add a support, or hang less on it.",
            "it carries less", load_share=share)
    return out


def _lighter_load_share(base: dict, cases, up, profile: Profile, shells, need: float) -> float:
    """Refine the suggested load near the target when the shape changes appreciably."""
    from dataclasses import replace

    from ..strength import evaluate_cases, large_motion

    share = base["safety_factor"] / need
    bends = any(c.get("geometry_checks", {}).get("bending", {}).get("status") == "solved" for c in base["cases"])
    if not bends and not any(large_motion(c, share * u) for c, u in zip(cases, base["_u_all"])):
        return share
    for _ in range(3):
        trial = [replace(c, forces=c.forces * share) for c in cases]
        observed = evaluate_cases(trial, up, profile, shells)["safety_factor"]
        if not (np.isfinite(observed) and observed > 0):
            break
        correction = observed / need
        if abs(correction - 1) < 5e-4:
            break
        share = min(1.0, share * correction)
    return share


def _scaled_safety(base: dict, p: Profile, other: Profile) -> float:
    """Safety factor of ``base`` (a strength result in ``p``'s material) printed in ``other``'s
    material instead: stresses barely depend on stiffness here, only the strengths change — at room
    temperature and at the temperature it's used at (service.heat_factor)."""
    from ..service import heat_factor
    from ..strength import failure_index, material_for

    cur, m = p.material, other.material
    k = heat_factor(other) / max(heat_factor(p), 1e-9)
    layer = cur["tensile_xy_mpa"] * p.z_strength_factor / (m["tensile_xy_mpa"] * other.z_strength_factor * k)
    material = cur["tensile_xy_mpa"] / (m["tensile_xy_mpa"] * k)
    buckling = material_for(p).e_xy / material_for(other).e_xy
    cases = base.get("case_fi", [(base["fi_layers_max"], base["fi_material_max"])])
    worst = max(failure_index(fi, layer, material, buckling) for fi in cases)
    return 1 / worst if worst > 0 else float("inf")


UV_WORDS = {"good": "made for sunlight", "fair": "fades a little in sunlight over the years", "poor": "turns brittle and yellow in sunlight"}


def material_advice(profile: dict | None = None, up=None, loads: list[dict] | None = None, outdoors: bool = False,
                    flexible: bool = False) -> list[dict]:
    """Which material to print it in: every material with "good" / "with care" / "not suitable" and
    the reasons — heat where it's used (profile ``use_temp_c``), sunlight (``outdoors``), a flexible
    part, what this printer can print (enclosure, hardened nozzle), and with ``up`` and ``loads``
    the safety factor for the load. Best first."""
    from ..profile import fibre
    from ..service import heat_factor, heat_limit, required_safety
    from ..strength import verdict

    p = Profile.from_dict(profile)
    base = None
    if up is not None and loads:
        from ..strength import evaluate_cases

        base = _evaluate(_load_case(loads), up, p, _shells(p))
    out = []
    for mid, m in presets()["materials"].items():
        other = Profile.from_dict({**(profile or {}), "material": mid})
        bad, care, good = [], [], []
        is_flexible = mid.startswith("tpu") or m.get("based_on", "").startswith("tpu")
        if flexible != is_flexible:
            bad.append("flexible (rubbery)" if is_flexible else "stiff, not flexible")
        elif flexible:
            good.append("flexible")
        t = p.use_temp_c
        if t is not None and t > 23:
            limit = heat_limit(other)
            if t >= limit:
                bad.append(f"goes soft around {limit:.0f} °C (you need {t:.0f} °C)")
            elif t >= limit - 15:
                care.append(f"close to where it goes soft (~{limit:.0f} °C)")
            else:
                good.append(f"takes the heat (soft only from ~{limit:.0f} °C)")
        if outdoors:
            uv = m.get("uv_resistance", "fair")
            (bad if uv == "poor" else care if uv == "fair" else good).append(UV_WORDS[uv])
        if m["abrasive"] and not p.printer.get("hardened_nozzle"):
            bad.append(f"needs a hardened nozzle (the {p.printer['name']} doesn't have one)")
        if m["warp_risk"] == "high" and p.enclosure == "none":
            care.append("warps on an open printer: needs an enclosure")
        elif m["warp_risk"] != "low" and p.enclosure == "none":
            care.append("may warp on an open printer")
        if m["hygroscopic"] == "high":
            care.append("keep the filament dry")
        if fibre(m):
            care.append(f"{fibre(m)} fibre: stiffer, not stronger: more brittle and weaker between the layers")
        sf = None
        if base is not None:
            sf = float(_scaled_safety(base, p, other))
            v = verdict(sf, required_safety(other))
            (good if v == "fine" else care if v == "borderline" else bad).append(f"holds your load {_format_safety(sf)}")
        rating = "not suitable" if bad else "with care" if care else "good"
        fuss = {"low": 0, "medium": 1, "high": 2}[m["warp_risk"]] + (m["hygroscopic"] == "high") + bool(m["abrasive"])
        out.append({"material": mid, "name": m["name"], "rating": rating, "safety_factor": sf,
                    "reasons": bad + care + good, "current": mid == p.material_id, "_fuss": fuss})
    order = {"good": 0, "with care": 1, "not suitable": 2}
    out.sort(key=lambda r: (order[r["rating"]], r["_fuss"], -(r["safety_factor"] or 0)))  # easiest of the good ones first
    for r in out:
        del r["_fuss"]
    return out
