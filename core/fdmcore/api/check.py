from __future__ import annotations

import numpy as np

from ..checks import RULE_IMPACT, orient, run_checks, slice_mesh
from ..formats import read_bodies
from ..mesh import Mesh
from ..messages import summarize_finding
from ..orientation import classify_faces
from ..placement import print_rotation
from ..profile import Profile, load_data, presets
from ..progress import stage
from ..recommend import goal_plan, print_risk, recommend_settings
from . import design as _design
from . import heat as _heat_mod
from . import orientation as _orient
from . import session as _S
from . import warping as _warp


# Support is printed sparse (~15 %) with denser interface layers under the part: about a quarter of
# the volume under the overhangs ends up as plastic (rough, like the support volume itself).
SUPPORT_FILL = 0.25


SPOOL_MARGIN = (1.1, 5.0)  # a print needs × this + g on the spool: estimate error, purge line and skirt


def _cost(oriented: Mesh, p: Profile, part_g: float, max_layers: int = 40) -> dict:
    """Support grams for this orientation and, with the user's price per kg / grams left on the spool
    (My materials), the cost and whether the spool will last."""
    from ..refine import support_stats

    support_g = support_stats(oriented, p, max_layers=max_layers)["support_volume"] * SUPPORT_FILL / 1000 * p.material["density_g_cm3"]
    total = part_g + support_g
    out = {"support_g": round(support_g, 1), "total_g": round(total, 1)}
    if p.material.get("price_per_kg"):
        out["cost"] = round(total / 1000 * p.material["price_per_kg"], 2)
    left = p.material.get("spool_left_g")
    if left is not None:
        need = total * SPOOL_MARGIN[0] + SPOOL_MARGIN[1]
        out["spool_left_g"], out["spool_short"] = float(left), bool(need > left)
    return out


def _check_for_design(up, profile: dict | None = None) -> tuple[list[dict], np.ndarray, list]:
    """Run only what design() needs from check(): findings and the sliced layers, skipping support
    cost, settings recommendations and face temperature."""
    s = _S._require()
    p = Profile.from_dict(profile)
    rotation, fit = print_rotation(s.mesh, up, p.printer["bed"], _orient._moving(p))
    oriented = orient(s.mesh, rotation)
    coarse = s.kind == "organic" and s.mesh.face_count >= _orient.DENSE_ORGANIC_FACES and not s.cad_checks
    layers = slice_mesh(oriented, p.layer_height, 80 if coarse else 160)
    result = run_checks(oriented, p, cad_checks=s.cad_checks, coarse=coarse, layers=layers)
    findings, _ = _finish_check_findings(s, oriented, rotation, up, p, fit, result, mode="print", faces=None, spots=None)
    return findings, rotation, layers


def _finish_check_findings(s: _S.Session, oriented: Mesh, rotation: np.ndarray, up, p: Profile, fit: dict,
                           result: dict, mode: str, faces: dict | None, spots) -> tuple[list[dict], dict]:
    """Post-process run_checks findings with session-level rules, risk, summaries and explanations.
    ``result["findings"]`` keeps the raw findings (recommend_settings reads them); returns (explained, risk)."""
    if spots is not None:
        result["findings"] += _heat_mod._spot_findings(spots, p, result["findings"])
    result["findings"] = _design._drop_rejected(result["findings"], s.mesh, rotation)
    if not s.cad_checks:
        cad_rules = set(load_data("thresholds")["organic"]["cad_rules"])
        result["findings"] = [f for f in result["findings"] if f["rule"] not in cad_rules]
    result["findings"] += _clean_face_findings(s.mesh, up, p, _orient._marked_faces(faces), rotation)
    result["findings"] += _thread_findings(oriented, rotation, p)
    result["findings"] += _grip_findings(s.name)
    if not fit["fits"]:
        result["findings"].insert(0, {"rule": "exceeds_build_volume", "severity": "problem", "bbox": None, "metrics": fit,
                                      "detail": f"Doesn't fit the {p.printer['name']}: {fit['reason']}"})
    elif fit["turned_deg"]:
        result["findings"].append({"rule": "turned_to_fit", "severity": "info", "bbox": None, "metrics": fit,
                                   "detail": f"Turned {fit['turned_deg']:.0f}° on the bed so it fits the {p.printer['name']}"})
    height = float(np.ptp(oriented.triangles[..., 2])) if len(oriented.triangles) else 0.0
    w = _warp._matching_warp(s, up, p)
    if w and w["peel_point"] is not None:
        result["findings"].append(_warp._adhesion_finding(s, w, rotation))
    risk = print_risk(p, result["findings"], height, w["verdict"] if w else None)
    if w and "recommended_hold" in w:
        risk["hold"] = w["recommended_hold"]
    if risk["level"] == "high":
        result["findings"].append({"rule": "print_may_fail", "severity": "warn", "bbox": None,
                                   "metrics": {"score": risk["score"], "hold": risk["hold"]},
                                   "detail": "The print may come loose or fall over on the bed: " + "; ".join(risk["reasons"])})
    for i, f in enumerate(result["findings"]):  # findings built here, outside run_checks, need impact + short text
        f.setdefault("impact", RULE_IMPACT.get(f["rule"], "info"))
        if "finding_id" not in f:
            result["findings"][i] = summarize_finding(f, p)
    findings = [explain_finding(f, mode) for f in result["findings"]]
    return emphasize_for_goal(findings, p.goal), risk


def check(up, profile: dict | None = None, mode: str = "print", faces: dict | None = None, detailed: bool = False) -> dict:
    """Run all printability checks for one orientation, explain them and recommend settings.
    ``faces`` as in ``orientations``: support on a face marked clean becomes a problem.
    ``detailed``: the heat check uses the real layer times of the loaded G-code (if it is for this orientation)."""
    s = _S._require()
    p = Profile.from_dict(profile)
    rotation, fit = print_rotation(s.mesh, up, p.printer["bed"], _orient._moving(p))
    oriented = orient(s.mesh, rotation)
    sliced = None
    if detailed and s.gcode is not None and np.dot(s.gcode["up"], np.asarray(up, float) / np.linalg.norm(up)) >= 0.999:
        from ..gcode import layer_times

        sliced = layer_times(s.gcode["toolpath"])
    coarse = s.kind == "organic" and s.mesh.face_count >= _orient.DENSE_ORGANIC_FACES and not s.cad_checks and not detailed
    result = run_checks(oriented, p, sliced=sliced, cad_checks=s.cad_checks, coarse=coarse)
    result["stats"]["heat_from_gcode"] = sliced is not None
    result["stats"].update(_cost(oriented, p, result["stats"]["filament_g"], max_layers=20 if coarse else 40))
    spots = _heat_mod._spot_heat(rotation, p) if sliced is not None else None
    findings, risk = _finish_check_findings(s, oriented, rotation, up, p, fit, result, mode=mode, faces=faces, spots=spots)
    return {
        "up": list(map(float, up)),
        "rotation": rotation.tolist(),
        "mode": mode,
        "findings": findings,
        "stats": result["stats"],
        "settings": recommend_settings(p, result["findings"], result["stats"], risk) if mode != "design" else [],
        "print_risk": risk,
        "z_strength_factor": result["z_strength_factor"],
        "face_temp": _heat_mod._face_temp(oriented, result["stats"]["layer_heat"], spots),
        "kind": s.kind,
        "cad_checks": s.cad_checks,
    }


def _grip_findings(name: str) -> list[dict]:
    """grip_texture when the file name says the part is held (a knob, handle, …: ``grip`` words in
    thresholds.yaml, at the start of a word so "radial" isn't a dial)."""
    import re

    words = load_data("thresholds")["grip"]["words"]
    hit = re.search(r"(?<![a-z])(" + "|".join(map(re.escape, words)) + ")", (name or "").lower())
    if not hit:
        return []
    return [{"rule": "grip_texture", "severity": "info", "bbox": None, "metrics": {"word": hit.group(1)},
             "detail": f"A {hit.group(1)} is held by hand: fuzzy skin on its outer walls gives it a grippy, matte surface "
                       "that also hides the layer lines"}]


def _thread_findings(oriented: Mesh, rotation: np.ndarray, p: Profile) -> list[dict]:
    """Threads modelled into the part (features.find_threads, along the model's own axes), with what
    this orientation and layer height do to them (modelled_thread)."""
    from ..features import find_threads

    T = load_data("thresholds")["threads"]
    lh = p.layer_height
    out = []
    for t in find_threads(oriented, axes=np.asarray(rotation).T):  # the model's x, y, z in the print frame (R's columns)
        what = (f"{t['size']} " if t["size"] else f"Ø{t['major_mm']:.1f} mm ") + ("bolt thread" if t["external"] else "threaded hole")
        upright = abs(t["axis"][2]) > 0.9
        lying = abs(t["axis"][2]) < 0.3
        layers = t["pitch_mm"] / lh
        tips = []
        bad = False
        if t["major_mm"] < T["min_major_mm"]:
            bad = True
            tips.append(f"threads smaller than M{T['min_major_mm']:g} print poorly: a heat-set insert, a nut, or tapping a plain "
                        "hole holds better")
        if upright and layers < T["min_layers_per_pitch"]:
            bad = True
            tips.append(f"standing up it gets only {layers:.1f} layers per turn of the thread ({t['pitch_mm']:g} mm pitch, "
                        f"{lh:g} mm layers): print it with layers of {t['pitch_mm'] / T['min_layers_per_pitch']:.2f} mm or "
                        "finer there (variable layer height)")
        if lying:
            bad = bad or t["major_mm"] < T["lying_ok_major_mm"]
            tips.append("it lies on its side, so its top and bottom come out as layer steps and the thread binds: stand it "
                        "up if you can")
        tips.append(f"leave about {T['clearance_mm']:g} mm between the printed bolt and nut, and screw it in and out a few "
                    "times to clean the thread before use")
        lo, hi = np.asarray(t["lo"]), np.asarray(t["hi"])
        out.append({"rule": "modelled_thread", "severity": "warn" if bad else "info", "bbox": [lo.tolist(), hi.tolist()],
                    "metrics": {"size": t["size"], "pitch_mm": t["pitch_mm"], "major_mm": t["major_mm"], "external": t["external"],
                                "layers_per_pitch": round(layers, 1), "upright": upright},
                    "detail": f"Modelled {what} ({t['pitch_mm']:g} mm pitch, {t['length_mm']:.0f} mm long): " + "; ".join(tips)})
    return out


def _clean_face_findings(mesh: Mesh, up, p: Profile, marked: dict | None, rotation: np.ndarray) -> list[dict]:
    """Support marks on a face the user wants clean: the one place they matter most. Our guess of the faces people
    see (``guessed``) only steers the orientation; the usual support_marks finding already names those marks."""
    if not marked or not marked["clean"] or marked.get("guessed"):
        return []
    from ..orientation import NEEDS_SUPPORT

    _, areas = mesh.normals_and_areas()
    classes = classify_faces(mesh, up, _orient.orientation_settings(p))
    placed = mesh.triangles @ rotation.T  # same placement as orient(): centred on x/y, lowest point at z = 0
    lo, hi = placed.reshape(-1, 3).min(axis=0), placed.reshape(-1, 3).max(axis=0)
    placed = placed + [-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]]
    out = []
    for m in marked["clean"]:
        hit = m & (classes == NEEDS_SUPPORT)
        if areas[hit].sum() < 0.05 * areas[m].sum():
            continue
        pts = placed[hit].reshape(-1, 3)
        out.append({"rule": "support_marks", "severity": "problem", "impact": "surface",
                    "bbox": [pts.min(axis=0).tolist(), pts.max(axis=0).tolist()],
                    "metrics": {"area": float(areas[hit].sum()), "clean_face": True},
                    "detail": f"Supports would touch the face you want clean (~{areas[hit].sum():.0f} mm²) — pick another orientation or accept marks there"})
    return out


def diagnose(symptom: str | None = None, up=None, profile: dict | None = None, findings: list[dict] | None = None) -> dict:
    """"My print failed": for what the user saw (``symptom``, a key of symptoms.yaml; None lists them), its usual
    causes and fixes, and ``on_your_part``: the findings that predicted it on this part - from ``findings``
    (the last check's, as the app has them) or a check in direction ``up``. Sorted most serious first."""
    data = load_data("symptoms")["symptoms"]
    if symptom is None:
        return {"symptoms": [{"id": k, "label": v["label"]} for k, v in data.items()]}
    if symptom not in data:
        raise ValueError(f"symptom must be one of {', '.join(data)}")
    entry = data[symptom]
    if findings is None and up is not None:
        findings = check(up, profile)["findings"]
    rank = {"problem": 0, "warn": 1, "info": 2}
    mine = sorted((f for f in findings or [] if f.get("rule") in entry["rules"]), key=lambda f: rank.get(f.get("severity"), 3))
    return {"symptom": symptom, "label": entry["label"], "why": entry["why"], "fixes": entry["fixes"], "sources": entry["sources"],
            "on_your_part": [{"rule": f["rule"], "title": f.get("title") or load_data("rules")["rules"][f["rule"]]["title"],
                              "detail": f.get("detail", ""), "severity": f.get("severity")} for f in mine]}


FIT_WORDS = {"press": "press together tightly", "snug": "push together snugly", "sliding": "slide and turn",
             "loose": "move freely with some play"}


CLEARANCE_MAP_MM = 1.0  # the heat map's range: touching (red) to this far apart (cool)


def clearance_map(data: bytes, profile: dict | None = None) -> dict:
    """The mating part heat map (T-573, plan §15.2 idea 3): ``clearance``'s answer, plus the gap painted on the loaded
    part: a finer copy of its surface (``field_triangles``, float32) with the gap to the mating part at every corner
    (``vertex_gap_mm``, float32, negative where they overlap, ``CLEARANCE_MAP_MM`` and more counts as free), the
    tightest spot on this part (``tight_at``) and the mating part itself (``mate_triangles``) to show beside it."""
    from ..clearance import signed_distance
    from ..formats import read_bodies
    from ..strength import refine

    out = clearance(data, profile)
    s = _S._require()
    mate = Mesh(np.concatenate([m.triangles for _, m in read_bodies(data)]))
    fine = refine(s.mesh.triangles, max(float(np.ptp(s.mesh.triangles.reshape(-1, 3), axis=0).max()) / 40, 1.0))
    corners = fine.reshape(-1, 3)
    uniq, back = np.unique(corners.round(4), axis=0, return_inverse=True)  # each corner is shared by several triangles
    d = signed_distance(uniq, mate)[0][np.ravel(back)]
    d = np.minimum(d, 10 * CLEARANCE_MAP_MM)
    # A fit is often equally tight all round (a peg in a hole): of the tightest spots the highest, at the mouth where
    # one looks in, not one at the bottom seen through the part.
    tight = np.flatnonzero(d <= d.min() + 0.02)
    i = int(tight[np.argmax(corners[tight, 2])])
    return {**out, "field_triangles": fine.astype(np.float32).tobytes(), "vertex_gap_mm": d.astype(np.float32).tobytes(),
            "tight_at": corners[i].round(3).tolist(), "tight_mm": round(float(d[i]), 3), "range_mm": CLEARANCE_MAP_MM,
            "mate_triangles": mate.triangles.astype(np.float32).tobytes()}


def clearance(data: bytes, profile: dict | None = None) -> dict:
    """The gap between the loaded part and a mating part (``data``: STL / 3MF / OBJ exported from the same CAD
    assembly, so both keep their places): the closest place (``gap_mm``, negative when they overlap; ``at``, part
    coordinates), the fit it gives (``verdict``: overlap, too tight, press, snug, sliding, loose, free) against the
    fit gaps (your fit test print if measured) and a plain ``explanation``."""
    from ..clearance import clearance as measure
    from ..formats import read_bodies

    s = _S._require()
    p = Profile.from_dict(profile)
    mate = Mesh(np.concatenate([m.triangles for _, m in read_bodies(data)]))
    gaps = {**load_data("thresholds")["fits"]["gap_mm"], **(p.material.get("fit_gap_mm") or {})}
    r = measure(s.mesh, mate, gaps)
    side = r["per_side_mm"]
    g = r["gap_mm"]
    if r["verdict"] == "overlap":
        text = f"The parts overlap by {-g:.2f} mm: they won't go together. Leave about {side['sliding']:.2f} mm per side for a sliding fit."
    elif r["verdict"] == "free":
        text = "The parts don't come closer than 1 mm anywhere: no fit between them to check."
    elif r["verdict"] == "too tight":
        text = (f"The closest gap is {g:.2f} mm per side: printed, that fuses or needs force. Leave {side['snug']:.2f} mm to push "
                f"together snugly, {side['sliding']:.2f} mm to slide and turn.")
    else:
        nxt = {"press": "snug", "snug": "sliding", "sliding": "loose"}.get(r["verdict"])
        text = f"The closest gap is {g:.2f} mm per side: the parts will {FIT_WORDS[r['verdict']]}."
        if nxt:
            text += f" For them to {FIT_WORDS[nxt]}, leave {side[nxt]:.2f} mm."
    measured = bool(p.material.get("fit_gap_mm"))
    text += " (Gaps from your fit test print.)" if measured else " (Typical gaps: “Settings & calibration” gives your printer's with one test print.)"
    return {**r, "explanation": text}


HOLE_COMP_MAX_MM = 0.3  # more than this per side points at a printer problem, not something to compensate


def tolerance(profile: dict | None = None) -> dict:
    """The fit gaps in use (``gaps``: typical, or ``measured`` with the fit test print) and the slicer hole
    compensation that follows from them (T-385): when this printer needs a bigger snug gap than a typical one,
    its holes print that much smaller, so every hole grows by half the difference per side (OrcaSlicer's
    X-Y hole compensation is per side). ``hole_compensation_mm`` is None until the fit test is measured."""
    p = Profile.from_dict(profile)
    typical = load_data("thresholds")["fits"]["gap_mm"]
    mine = p.material.get("fit_gap_mm") or {}
    gaps = {**typical, **mine}
    comp = None
    if "snug" in mine:
        comp = round(min(max((mine["snug"] - typical["snug"]) / 2, 0.0), HOLE_COMP_MAX_MM), 2)
    if comp is None:
        text = (f"Typical gaps: {gaps['snug']:.2f} mm to push in snugly, {gaps['sliding']:.2f} mm to slide. Print the fit"
                " test once to get your printer's, and the hole compensation that goes with them.")
    elif comp == 0:
        text = (f"From your fit test: {gaps['snug']:.2f} mm snug, {gaps['sliding']:.2f} mm sliding. Your holes come out"
                " true to size: no hole compensation needed.")
    else:
        text = (f"From your fit test: {gaps['snug']:.2f} mm snug, {gaps['sliding']:.2f} mm sliding. Your holes print about"
                f" {2 * comp:.2f} mm small: hole compensation {comp:.2f} mm makes them right for parts you can't change.")
    return {"gaps": gaps, "measured": bool(mine), "hole_compensation_mm": comp, "explanation": text}


def setup_check(profile: dict | None = None, settings: dict | None = None) -> dict:
    """Mistakes in the chosen printer / material / nozzle (presets the user made) and in slicer values
    they set (``settings``, slicer keys such as layer_height, support_top_z_distance, outer_wall_speed):
    explained findings, no part needed."""
    from ..checks import setup_findings

    return {"findings": [explain_finding(f) for f in setup_findings(Profile.from_dict(profile), settings)]}


def explain_finding(finding: dict, mode: str = "both") -> dict:
    has_id = bool(finding.get("finding_id"))
    base = finding if ("message" in finding and has_id) else summarize_finding(finding)
    rule = load_data("rules")["rules"].get(base["rule"], {})
    domains = _S.MODES[mode]
    all_fixes = rule.get("fixes", [])
    fixes = [dict(f) for f in all_fixes if f["domain"] in domains]
    if mode == "print":
        fixes.sort(key=lambda f: {"slicer": 0, "process": 1}[f["domain"]])
    if has_id:
        fid = base["finding_id"]
        for fx in fixes:
            fx["finding_id"] = fid
    return {
        **base,
        "title": rule.get("title", base["rule"]),
        "why": rule.get("why", ""),
        "fixes": fixes,
        # Fix kinds left out by the mode, so the UI can say "more fixes if you can change the model".
        "hidden_fixes": sorted({f["domain"] for f in all_fixes if f["domain"] not in domains}),
    }


# How the goal shifts severity, per impact group: +1 = one step more serious, -1 = less.
# A strong part can't afford weak spots; a fast or cheap print accepts rougher surfaces
# and weak spots the user has been told about.
GOAL_EMPHASIS = {
    "strength": {"weak": +1},
    "speed": {"weak": -1, "surface": -1},
    "material": {"weak": -1, "surface": -1},
}


_SEVERITIES = ["info", "warn", "problem"]


_GOAL_NAME = {"strength": "Strength", "speed": "Speed", "material": "Material"}


def emphasize_for_goal(findings: list[dict], goal: str) -> list[dict]:
    """Raise or lower severities to match what the user optimises for, then re-sort."""
    from ..checks import IMPACT_ORDER, SEVERITY_ORDER

    shift = GOAL_EMPHASIS.get(goal, {})
    out = []
    for f in findings:
        step = shift.get(f.get("impact"), 0)
        k = _SEVERITIES.index(f["severity"])
        new = _SEVERITIES[min(max(k + step, 0), len(_SEVERITIES) - 1)]
        if new != f["severity"]:
            word = "More important" if step > 0 else "Less important"
            f = {**f, "severity": new, "goal_note": f"{word} because you optimise for {_GOAL_NAME[goal]}"}
        out.append(f)
    out.sort(key=lambda f: (IMPACT_ORDER.index(f.get("impact", "info")), SEVERITY_ORDER[f["severity"]]))
    return out


def compare_nozzles(up, profile: dict | None = None) -> list[dict]:
    """The same part with every nozzle: print time, filament, the nozzle-dependent problems (``new``:
    not there with the smallest nozzle) and the detail it can't trace (checks.lost_detail)."""
    from ..checks import NOZZLE_RULES, lost_detail, nozzle_comparison_check
    from ..holes import horizontal_holes

    s = _S._require()
    base = Profile.from_dict(profile)
    rotation, _ = print_rotation(s.mesh, up, base.printer["bed"], _orient._moving(base))
    oriented = orient(s.mesh, rotation)
    coarse = s.kind == "organic" and s.mesh.face_count >= _orient.DENSE_ORGANIC_FACES and not s.cad_checks
    max_layers = 80
    detail_layers = slice_mesh(oriented, 0.2, max_layers)  # one set of layers, so only the nozzle differs
    hholes = horizontal_holes(oriented)
    surface = oriented.normals_and_areas()
    out = []
    nozzles = sorted(presets()["nozzles"], key=float)
    for i, nozzle in enumerate(nozzles):
        p = Profile.from_dict({**(profile or {}), "nozzle": nozzle})
        reuse_layers = detail_layers if abs(p.layer_height - 0.2) < 1e-6 else None
        with stage(i / len(nozzles), (i + 1) / len(nozzles)):
            r = nozzle_comparison_check(oriented, p, max_layers=max_layers, cad_checks=s.cad_checks,
                                        coarse=coarse, hholes=hholes, layers=reuse_layers, surface=surface)
        issues = [explain_finding(f) for f in r["findings"] if f["rule"] in NOZZLE_RULES]
        out.append({
            "nozzle": nozzle,
            "print_time_min": r["stats"]["print_time_min"],
            "filament_g": r["stats"]["filament_g"],
            "issues": [{
                "rule": f["rule"], "title": f["title"], "detail": f["detail"],
                "finding_id": f["finding_id"], "short_title": f["short_title"],
                "one_liner": f["one_liner"], "instance_label": f["instance_label"],
                "message": f["message"],
            } for f in issues],
            "current": nozzle == base.nozzle,
            "walls": goal_plan(p)["walls"],
            **lost_detail(detail_layers, float(nozzle)),
        })
    first = {f["title"] for f in out[0]["issues"]}
    for row in out:
        for f in row["issues"]:
            f["new"] = f["title"] not in first
    # Each nozzle with its own usual settings. Bigger nozzles' profiles print slower, and a wider line
    # holds more plastic per mm, so once the hotend melts as fast as it can the nozzle moves slower:
    # on small parts a bigger nozzle often saves little time (OrcaSlicer's own profiles: L-bracket
    # 27 / 22 / 23 min with 0.4 / 0.6 / 0.8 mm on a P1S).
    for small, big in zip(out, out[1:]):
        if big["print_time_min"] >= 0.95 * small["print_time_min"]:
            big["note"] = (f"Not faster than {small['nozzle']} mm on this part: slicer profiles run bigger nozzles slower, "
                           f"and your hotend melts only about {base.material['max_flow_mm3s']:g} mm³/s of {base.material['name']}, "
                           "so wide lines can't move fast. Bigger nozzles save time on big, chunky parts")
    return out
