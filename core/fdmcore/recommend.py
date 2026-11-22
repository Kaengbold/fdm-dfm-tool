"""Slicer settings recommendation from profile + findings.

Keys are OrcaSlicer / Bambu Studio setting names so they can go straight into a
3MF. Each recommendation carries a plain-language reason.
"""

from __future__ import annotations

from bisect import bisect_right
from math import ceil

from .profile import Profile, load_data, presets

# What each goal changes in the slicer. Walls carry most of the strength (2–3× more than
# infill), thicker layers print faster, sparse infill patterns save plastic.
GOAL_PLANS = {
    "balanced": {"walls": 3, "infill": 15, "pattern": "gyroid", "layer_factor": 1.0, "top": 4, "bottom": 3},
    "strength": {"walls": 4, "infill": 25, "pattern": "gyroid", "layer_factor": 1.0, "top": 5, "bottom": 4},
    "speed": {"walls": 2, "infill": 10, "pattern": "grid", "layer_factor": 1.4, "top": 3, "bottom": 3},
    "material": {"walls": 2, "infill": 8, "pattern": "lightning", "layer_factor": 1.0, "top": 3, "bottom": 3},
}
PATTERN_REASON = {
    "gyroid": "Strong in all directions and prints quietly",
    "grid": "Fast to print and stiff enough for most parts",
    "lightning": "Uses the least plastic, only holds up the top surface. Not for parts that carry a load",
}

REF_LINE_MM, REF_LAYER_MM = 0.45, 0.2  # the 0.4 mm nozzle the plans above are written for
GOAL_NAMES = {"balanced": "Balanced", "strength": "Strong", "speed": "Fast", "material": "Light"}  # as the app shows them


def part_fact(findings: list[dict], *rules: str) -> str:
    """This part's measured fact behind a setting: the start of the first matching finding's detail
    ("Only 35 mm² touches the bed"), so the reason names the part instead of repeating a generic line.
    Empty when none of ``rules`` was found."""
    for f in findings:
        if f["rule"] in rules:
            fact = f.get("detail", "").split(" — ")[0].split(" (")[0].split(": its top")[0].strip()
            return fact[:1].lower() + fact[1:] if fact[1:2].islower() else fact
    return ""


def grip_settings(profile: Profile) -> dict[str, str]:
    """Fuzzy skin on the outer walls (``grip`` in thresholds.yaml), for a part people hold. OrcaSlicer's
    "combined" generator keeps the wall closed (plain displacement opens pores in it) and needs the Arachne
    wall generator; Bambu Studio and PrusaSlicer don't know it and displace the wall instead."""
    G = load_data("thresholds")["grip"]
    thick = min(G["thickness_mm"], 0.75 * profile.line_width)
    return {"fuzzy_skin": "external", "fuzzy_skin_thickness": f"{thick:.2f}", "fuzzy_skin_point_distance": f"{G['point_distance_mm']:g}",
            "fuzzy_skin_mode": "combined", "wall_generator": "arachne"}


def goal_plan(profile: Profile) -> dict:
    """Walls, infill (%), pattern, layer height and top/bottom layers for the user's goal.
    The plans are written for a 0.4 mm nozzle; other nozzles keep the same wall and top/bottom
    thickness in mm with their own line width and layer height (three 0.45 mm walls → two 0.82 mm
    ones), so the part is as strong and a nozzle comparison is fair."""
    plan = dict(GOAL_PLANS[profile.goal])
    lh = profile.layer_height * plan.pop("layer_factor")
    plan["layer_height"] = round(min(lh, 0.7 * float(profile.nozzle)) / 0.02) * 0.02
    plan["layer_height"] = round(plan["layer_height"], 2)
    plan["walls"] = max(2, round(plan["walls"] * REF_LINE_MM / profile.line_width))
    layers = REF_LAYER_MM / profile.layer_height
    plan["top"] = max(3, round(plan["top"] * layers))
    plan["bottom"] = max(2, round(plan["bottom"] * layers))
    return plan


def _sparse_top(plan: dict, stats: dict, S: dict, reasons: dict[str, str]) -> None:
    """A sparse foundation needs one more closing layer, including on sloping tops."""
    if plan["infill"] > S["low_infill_percent"]:
        return
    plan["top"] += S["low_infill_extra_top"]
    area = stats.get("top_flat_mm2", 0)
    fact = f"This part has a {area:,.0f} mm² flat top" if area > 0 else "This part's top surfaces need closing"
    reasons["top"] = f"{fact} over {plan['infill']}% infill; one extra solid top layer helps close the gaps"


def _shape_layers(plan: dict, profile: Profile, stats: dict, findings: list[dict], reasons: dict[str, str]) -> None:
    """Keep fine layers local where possible; speed up only a measured plain shape."""
    S = load_data("thresholds")["shape_layers"]
    area = stats.get("visible_surface_mm2", 0)
    if not area:
        return
    nozzle, old = float(profile.nozzle), plan["layer_height"]
    slopes = [f["metrics"] for f in findings if f["rule"] == "shallow_slope_stairs"]
    rough = [f for f in findings if f["rule"] == "fine_detail_rough"]
    detail = stats.get("fine_detail_surface_mm2", 0)
    detail_used = bool(rough) and detail / area >= S["detail_surface_share"]
    slope_area = sum(r["area_mm2"] for r in slopes)
    height = stats["height_mm"]
    if slopes:
        plan["fine_layers"] = slopes
    if detail_used:
        plan["layer_height"] = min(old, round(nozzle * S["detail_nozzle_ratio"], 2))
        reasons["layer_height"] = (f"This part has about {detail:,.0f} mm² of fine-detail sides ({detail / area:.0%} of its visible surface): "
                                   f"{part_fact(findings, 'fine_detail_rough')}; thinner layers reduce vertical steps, "
                                   "but narrow details still need Arachne or a smaller nozzle")
    elif slopes:
        bands = ", ".join(f"{r['z0']:g} to {r['z1']:g} mm" for r in slopes)
        if slope_area / area >= S["slope_surface_share"] and sum(r["z1"] - r["z0"] for r in slopes) / height >= S["whole_height_share"]:
            plan["layer_height"] = min(old, round(nozzle * S["slope_nozzle_ratio"], 2))
            reasons["layer_height"] = f"Shallow slopes cover {slope_area:,.0f} mm² ({slope_area / area:.0%}) of this part's visible surface over {bands}; thinner layers reduce the steps"
        else:
            reasons["layer_height"] = (f"This part's shallow slopes cover {slope_area:,.0f} mm²; keep {old:g} mm elsewhere. "
                                       f"Use thin layers only from {bands} (the height ranges in the 3MF)")
    else:
        # An XY detail warning is not cured by thin layers, but it rules out sacrificing detail for speed.
        detail_rules = {"too_thin_to_print", "fine_detail_rough", "single_line_wall", "hole_closes", "gap_fuses",
                        "first_layer_detail", "shallow_relief", "side_detail_small", "modelled_thread", "sideways_hole_oval",
                        "small_hole_undersized", "weak_upright_pin", "thin_neck", "snap_fit_upright", "living_hinge"}
        if (profile.goal in ("balanced", "speed") and not any(f["rule"] in detail_rules for f in findings)
                and stats.get("plain_surface_share", 0) >= S["plain_surface_share"]
                and stats.get("wall_min_mm", 0) >= S["min_wall_lines"] * profile.line_width):
            ratio = S["fast_nozzle_ratio"] if profile.goal == "speed" else S["balanced_nozzle_ratio"]
            plan["layer_height"] = max(old, round(nozzle * ratio, 2))
            reasons["layer_height"] = (f"This part has {area:,.0f} mm² of flat or upright surface; its thinnest walls measure about {stats['wall_min_mm']:.2f} mm; "
                                       f"no slopes or fine-detail warnings need thinner layers, so {GOAL_NAMES[profile.goal]} can print thicker layers")
    # Finer layers must not quietly make top/bottom skins thinner.
    if plan["layer_height"] < old:
        for key in ("top", "bottom"):
            skin = plan[key] * old
            plan[key] = ceil(skin / plan["layer_height"] - 1e-6)
            reason = (f"Finer layers for this part's {detail if detail_used else slope_area:,.0f} mm² of detail; "
                      f"keep at least {skin:.2f} mm of solid {key} with {plan['layer_height']:g} mm layers")
            reasons[key] = f"{reasons[key]}. {reason}" if key in reasons else reason


def shape_plan(profile: Profile, stats: dict, findings: list[dict]) -> tuple[dict, dict[str, str]]:
    """Start with the goal; change only what the measured walls/interior ask for.

    Missing measurements keep the old plan, including its weak-spot safeguard. The size bins
    are practical starting densities, not a prediction of how much load the part can carry.
    """
    plan = goal_plan(profile)
    reasons = {}
    S = load_data("thresholds")["shape_settings"]
    weak = ("weak_upright_pin", "thin_neck", "single_line_wall")
    if any(f["rule"] in weak for f in findings) and plan["walls"] < 4 and profile.goal not in ("speed", "material"):
        plan["walls"] = 4
        reasons["walls"] = f"Raised to 4 for a weak spot: {part_fact(findings, *weak)}"
    if "wall_typical_mm" not in stats:
        _sparse_top(plan, stats, S, reasons)
        _shape_layers(plan, profile, stats, findings, reasons)
        return plan, reasons

    typical, thinnest, upper = (stats[k] for k in ("wall_typical_mm", "wall_min_mm", "wall_upper_mm"))
    core_share, core_cm3 = stats["infill_volume_share"], stats["infill_volume_cm3"]
    lw = profile.line_width
    goal_walls = goal_plan(profile)["walls"]
    fact = f"Your walls are typically {typical:.2f} mm (thinnest about {thinnest:.2f} mm)"
    if core_share <= S["solid_core_share"]:
        reasons["infill"] = f"Only {core_share:.0%} of this part is left for infill after walls and solid surfaces; infill is hardly used here"
        if upper <= 2 * goal_walls * lw + 1e-6:
            plan["walls"] = max(1, ceil(upper / (2 * lw) - 1e-6))
            reasons["walls"] = (f"{fact}; {plan['walls']} wall loops from each side fill them solid "
                                f"with {lw:.2f} mm lines (infill is hardly used here)")
    else:
        # Mostly walls with a small interior: finish filling the wall rather than leaving narrow gaps.
        if core_share <= S["mixed_core_share"] and 2 * goal_walls * lw < typical <= 2 * (goal_walls + 1) * lw:
            plan["walls"] = max(plan["walls"], ceil(typical / (2 * lw) - 1e-6))
            reasons["walls"] = f"{fact}; {plan['walls']} loops from each side fill the typical wall instead of leaving a narrow gap"
        size = bisect_right(S["core_size_cm3"], core_cm3)
        plan["infill"] = max(S["min_infill_percent"], min(S["max_infill_percent"], plan["infill"] + S["density_offset"][size]))
        reasons["infill"] = (f"About {core_cm3:.1f} cm³ ({core_share:.0%}) inside this part is left for infill; "
                             f"{plan['infill']}% backs up that interior for your goal {GOAL_NAMES[profile.goal]}")
        if "walls" not in reasons:
            reasons["walls"] = (f"{fact}; keep {plan['walls']} loops from your goal {GOAL_NAMES[profile.goal]}. "
                                f"The {core_cm3:.1f} cm³ interior needs infill too; fewer walls would not fill it")
    _sparse_top(plan, stats, S, reasons)
    _shape_layers(plan, profile, stats, findings, reasons)
    return plan, reasons


_SEV = {"info": 0, "warn": 1, "problem": 2}


def print_risk(profile: Profile, findings: list[dict], height_mm: float, warp_verdict: str | None = None) -> dict:
    """How likely the whole print fails on the bed: comes loose at the corners, tips over or splits (T-391).
    Adds up the signs the check found (warping, a small base, a tall narrow part, a shrinking plastic on an
    open printer) and the warping simulation's verdict when it ran (``warp_verdict``), into ``level`` low /
    medium / high with plain ``reasons``, and what holds it down: ``hold`` no_brim / brim_ears / outer_only / raft.
    A raft only as the last resort: a tiny base that also warps or tips, or a part the simulation says will
    lift in a shrinking plastic on an open printer."""
    R = load_data("thresholds")["print_risk"]
    worst: dict[str, int] = {}
    for f in findings:
        worst[f["rule"]] = max(worst.get(f["rule"], -1), _SEV.get(f.get("severity", "info"), 0))
    mat, open_printer = profile.material, profile.enclosure == "none"
    score, reasons = 0, []
    warps = worst.get("warping", -1) >= 1 or warp_verdict in ("might lift", "likely to lift")
    if warp_verdict == "likely to lift" or worst.get("warping", -1) >= 2:
        score += 2
        reasons.append("the corners will likely lift off the bed" + (" (warping simulation)" if warp_verdict == "likely to lift" else ""))
    elif warps:
        score += 1
        reasons.append("the corners might lift off the bed")
    tiny = worst.get("small_bed_contact", -1) >= 1
    if tiny:
        score += 1
        reasons.append(f"little of it touches the bed ({part_fact(findings, 'small_bed_contact')})")
    tips = "tipping_risk" in worst
    if tips:
        score += 1
        reasons.append(f"it is tall on a narrow base ({part_fact(findings, 'tipping_risk')})")
    if mat.get("warp_risk") == "high" and open_printer and height_mm > R["split_height_mm"]:
        score += 1
        reasons.append(f"{mat['name']} {height_mm:.0f} mm tall on an open printer can split between layers as it cools")
    if (tiny and (tips or warps)) or (warp_verdict == "likely to lift" and mat.get("warp_risk") == "high" and open_printer):
        hold = "raft"
    elif "small_bed_contact" in worst or tips:  # a brim for any small base, even one only noted
        hold = "outer_only"
    elif "warping" in worst or warps:
        hold = "brim_ears"
    else:
        hold = "no_brim"
    return {"level": "low" if score == 0 else "medium" if score == 1 else "high", "score": score, "reasons": reasons, "hold": hold}


# Speeds a cooling plan may set per height, and their OrcaSlicer keys (export3mf.PRUSA_KEYS has PrusaSlicer's).
COOLING_SPEEDS = {"outer_wall": "outer_wall_speed", "inner_wall": "inner_wall_speed", "sparse_infill": "sparse_infill_speed",
                  "internal_solid_infill": "internal_solid_infill_speed", "top_surface": "top_surface_speed"}


def cooling_ranges(plan: dict) -> list[dict]:
    """A cooling plan (the heat optimizer's fixes, T-480: ``ranges`` = [{"z0", "z1", "speeds": {part of the
    print: mm/s}}]) as height ranges for the user's slicer (export3mf.range_files). Only speeds go per height:
    OrcaSlicer, Bambu Studio and PrusaSlicer all apply them, but none takes a fan change per height from a
    project file (Bambu Studio ignores it, the others drop it at their next fan change: tools/slicer_check.py),
    and the slicer's own slowdown for layer cooling stays on."""
    out = []
    for r in plan.get("ranges") or []:
        z0, z1 = float(r["z0"]), float(r["z1"])
        if not z1 > z0 >= 0:
            raise ValueError(f"A cooling range must go up from z0 to z1 (got {z0}–{z1} mm)")
        if "fan" in r:
            raise ValueError("A fan change per height can't go into a slicer project: only speeds can")
        speeds = r.get("speeds") or {}
        unknown = sorted(set(speeds) - set(COOLING_SPEEDS))
        if unknown:
            raise ValueError(f"Unknown speed: {', '.join(unknown)} (use {', '.join(COOLING_SPEEDS)})")
        if any(float(v) <= 0 for v in speeds.values()):
            raise ValueError("Speeds must be above 0 mm/s")
        if speeds:
            out.append({"z0": z0, "z1": z1, **{COOLING_SPEEDS[k]: float(v) for k, v in speeds.items()}})
    return out


def bambu(printer: dict) -> bool:
    """A Bambu Lab printer, so most likely sliced in Bambu Studio. Its brim ears are a painting tool, not a
    brim type: given ``brim_ears`` in a 3MF it drops the object's brim settings and prints no brim at all
    (Bambu Studio 1.8, found by tools/slicer_check.py, T-493). Those printers get an outer brim instead."""
    return str(printer.get("name", "")).startswith("Bambu Lab")


def _finding_ids(findings: list[dict], *rules: str) -> list[str]:
    """Stable finding_ids in ``findings`` whose rule is in ``rules`` (deduplicated, order preserved)."""
    seen: list[str] = []
    for f in findings:
        fid = f.get("finding_id")
        if f.get("rule") in rules and fid and fid not in seen:
            seen.append(fid)
    return seen


def recommend_settings(profile: Profile, findings: list[dict], stats: dict | None = None, risk: dict | None = None) -> list[dict]:
    """``risk``: print_risk for this part; left out, it is worked out from the findings alone."""
    rules = {f["rule"] for f in findings}
    stats = stats or {}
    mat = profile.material
    T = load_data("thresholds")
    plan, shape_reasons = shape_plan(profile, stats, findings)
    # Send the actual goal baseline with each row; the browser must not guess slicer defaults.
    base = goal_plan(profile)
    usual = {"layer_height": str(base["layer_height"]), "wall_loops": str(base["walls"]),
             "sparse_infill_density": f"{base['infill']}%", "sparse_infill_pattern": base["pattern"],
             "top_shell_layers": str(base["top"]), "bottom_shell_layers": str(base["bottom"])}
    out: list[dict] = []

    def put(key, value, label, reason, fixes: tuple[str, ...] = ()):
        ids = _finding_ids(findings, *fixes) if fixes else []
        out.append({"key": key, "value": str(value), "label": label, "reason": reason, "usual": usual.get(key),
                    "finding_id": ids[0] if ids else None, "finding_ids": ids})

    goal = profile.goal
    # Values that come from the goal alone say so; values this part asked for name its measured fact.
    from_goal = f"From your goal {GOAL_NAMES.get(goal, goal)}; nothing on this part asks for more"
    lh_fixes = ("fine_detail_rough", "shallow_slope_stairs") if (plan["layer_height"] < base["layer_height"] or "fine_layers" in plan) else ()
    put("layer_height", plan["layer_height"], "Layer height",
        shape_reasons.get("layer_height", f"From your goal {GOAL_NAMES.get(goal, goal)} for a {profile.nozzle} mm nozzle; nothing on this part asks for finer layers"),
        fixes=lh_fixes)

    walls, infill = plan["walls"], plan["infill"]
    weak_walls = shape_reasons.get("walls", "").startswith("Raised to 4 for a weak spot")
    put("wall_loops", walls, "Walls", shape_reasons.get("walls", from_goal),
        fixes=("weak_upright_pin", "thin_neck", "single_line_wall") if weak_walls else ())
    put("sparse_infill_density", f"{infill}%", "Infill", shape_reasons.get("infill", from_goal))
    put("sparse_infill_pattern", plan["pattern"], "Infill pattern", PATTERN_REASON[plan["pattern"]])
    put("top_shell_layers", plan["top"], "Top layers", shape_reasons.get("top", from_goal))
    put("bottom_shell_layers", plan["bottom"], "Bottom layers", shape_reasons.get("bottom", from_goal))

    overhang = stats.get("support_area_mm2", 0)
    if stats.get("needs_support") or "long_bridge" in rules:
        sup_fixes = ("long_bridge", "support_marks")
        put("enable_support", 1, "Supports", f"{overhang:,.0f} mm² hang in the air in this orientation" if overhang >= 1
            else f"Holds up a long bridge: {part_fact(findings, 'long_bridge')}", fixes=sup_fixes)
        put("support_type", "tree(auto)", "Support type", "Tree supports use less material and break off easily", fixes=sup_fixes)
        # Orca's threshold is measured from the horizontal; ours from vertical.
        put("support_threshold_angle", round(90 - mat["max_overhang_deg"]), "Support threshold",
            f"{mat['name']} handles overhangs up to about {mat['max_overhang_deg']}° from vertical", fixes=sup_fixes)
        if profile.multi_material:
            put("support_interface_pattern", "rectilinear_interlaced", "Support interface",
                "Dense interface; print it in a material that doesn't bond to the part (e.g. PETG for PLA) for clean undersides",
                fixes=sup_fixes)
    else:
        put("enable_support", 0, "Supports", f"Only {overhang:,.0f} mm² overhang, little enough to print without" if overhang >= 1
            else "Nothing hangs in the air in this orientation")

    risk = risk or print_risk(profile, findings, stats.get("height_mm", 0.0))
    if risk["hold"] == "raft":
        raft_fixes = ("print_may_fail", "small_bed_contact", "tipping_risk", "warping")
        put("raft_layers", T["print_risk"]["raft_layers"], "Raft",
            "A few layers of plastic under the part hold it down where a brim isn't enough: " + "; ".join(risk["reasons"])
            + ". It leaves a rough underside: cut it off after printing", fixes=raft_fixes)
        put("brim_type", "no_brim", "Brim", "The raft holds the part instead")
    elif risk["hold"] == "outer_only":
        width = T["warping"]["brim_width_mm"] if "warping" in rules or mat["warp_risk"] == "high" else 5
        brim_fixes = ("small_bed_contact", "tipping_risk", "warping")
        put("brim_type", "outer_only", "Brim", f"Holds the part down: {part_fact(findings, 'small_bed_contact', 'tipping_risk')}", fixes=brim_fixes)
        put("brim_width", width, "Brim width", "Wide enough to stop corners lifting", fixes=brim_fixes)
    elif risk["hold"] == "brim_ears":
        # Only the corners lift: ears at the sharp corners hold them with far less brim to cut off
        # (OrcaSlicer wiki, Brim: Mouse ears). PrusaSlicer has none and gets an outer brim instead (export3mf);
        # so does Bambu Studio, where ears are painted by hand (see ``bambu``).
        fact = part_fact(findings, "warping") or "the warping simulation"
        if bambu(profile.printer):
            put("brim_type", "outer_only", "Brim", f"Holds the corners down, where warping starts ({fact}). A full brim: "
                "Bambu Studio has no brim ears setting (you can paint ears there by hand instead)", fixes=("warping",))
        else:
            put("brim_type", "brim_ears", "Brim", f"Ears at the corners, where warping starts ({fact}); less to cut off than a full brim",
                fixes=("warping",))
        put("brim_width", T["warping"]["brim_width_mm"], "Brim width", "Wide enough to stop corners lifting", fixes=("warping",))
    else:
        put("brim_type", "no_brim", "Brim", "Enough bed contact without one")

    if stats.get("uneven_wall_share", 0) >= T["shape_settings"]["uneven_wall_share"]:
        put("wall_generator", "arachne", "Wall generator",
            f"About {stats['uneven_wall_share']:.0%} of this part's wall samples, around {stats['uneven_wall_mm']:.2f} mm wide, "
            f"fall between whole {profile.line_width:.2f} mm lines; Arachne varies line width to fill them",
            fixes=("wall_line_mismatch", "too_thin_to_print", "fine_detail_rough", "single_line_wall"))
    elif rules & {"too_thin_to_print", "fine_detail_rough", "single_line_wall"}:
        put("wall_generator", "arachne", "Wall generator", "Arachne varies line width so thin details still print: "
            + part_fact(findings, "too_thin_to_print", "fine_detail_rough", "single_line_wall"),
            fixes=("too_thin_to_print", "fine_detail_rough", "single_line_wall"))
    # Measured on the user's own prints (My materials): corrects every hole / outline, measured beats the default.
    hole_err, outer_err = mat.get("hole_error_mm"), mat.get("outer_error_mm")
    if hole_err is not None and abs(hole_err) >= T["holes"]["measured_min_mm"]:
        put("xy_hole_compensation", round(-hole_err / 2, 3), "Hole compensation",
            f"Your prints make holes {abs(hole_err):.2f} mm {'smaller' if hole_err < 0 else 'bigger'} than modelled (you measured it): this corrects them",
            fixes=("small_hole_undersized", "hole_closes"))
    if outer_err is not None and abs(outer_err) >= T["holes"]["measured_min_mm"]:
        put("xy_contour_compensation", round(-outer_err / 2, 3), "Outer size compensation",
            f"Your prints come out {abs(outer_err):.2f} mm {'bigger' if outer_err > 0 else 'smaller'} outside than modelled (you measured it): this corrects it")
    if "small_hole_undersized" in rules and not any(s["key"] == "xy_hole_compensation" for s in out):
        put("xy_hole_compensation", T["holes"]["xy_hole_compensation_mm"], "Hole compensation",
            f"Opens up small holes: {part_fact(findings, 'small_hole_undersized')}", fixes=("small_hole_undersized",))
        put("hole_to_polyhole", 1, "Polyholes", "Prints round holes as polygons with fewer, longer sides: they come out much closer to size (OrcaSlicer only: Bambu Studio and PrusaSlicer ignore it)",
            fixes=("small_hole_undersized",))
    if rules & {"small_layers_cooling", "tall_thin_feature"}:
        put("slow_down_for_layer_cooling", 1, "Slow down small layers",
            f"Gives small layers time to cool: {part_fact(findings, 'small_layers_cooling', 'tall_thin_feature')}",
            fixes=("small_layers_cooling", "tall_thin_feature"))
    # The first layer is squashed onto the bed and bulges out; pulling it in a little keeps bottom
    # edges straight. Most printer profiles ship 0.1-0.2 mm; harmless when the profile already has it.
    if risk["hold"] == "raft":  # the part's first layer lies on the raft, not squashed onto the bed
        put("elefant_foot_compensation", 0, "Elephant foot compensation", "Not needed on a raft: the part isn't squashed onto the bed")
    else:
        put("elefant_foot_compensation", T["first_layer"]["elephant_foot_compensation_mm"], "Elephant foot compensation",
            "Keeps the bottom edges and holes at the bed true to size" if rules & {"elephant_foot", "first_layer_detail"}
            else "The first layer gets squashed and bulges out; this pulls it in so the bottom edge stays straight",
            fixes=("elephant_foot", "first_layer_detail"))
    looks = T["looks"]
    if stats.get("top_flat_mm2", 0) >= T["ironing"]["min_top_area_mm2"] and (goal in ("balanced", "strength") or profile.looks):
        how = looks["ironing_type"] if profile.looks else "topmost"
        put("ironing_type", how, "Ironing", f"The {stats['top_flat_mm2']:,.0f} mm² flat top comes out smooth: the nozzle passes"
            " over it once more with almost no plastic (adds a few minutes)" + (", and so do the lower flat tops" if how == "top" else ""))
        measured = mat.get("ironing_flow_pct")
        if measured is not None or profile.looks:
            put("ironing_flow", f"{measured if measured is not None else looks['ironing_flow_pct']:g}%", "Ironing flow",
                "From your ironing test" if measured is not None else "A common starting point: the ironing test finds your own")
            put("ironing_spacing", looks["ironing_spacing_mm"], "Ironing spacing", "Close passes for a smooth top")
    if profile.looks:  # Quality (plan §14): sourced in research/quality_settings.md
        base = presets()["speeds"][profile.printer.get("speed", "fast")]["outer_wall"]
        share = presets()["nozzles"].get(profile.nozzle, {}).get("speed", {}).get("outer_wall", 1.0)
        put("outer_wall_speed", round(base * share * looks["outer_wall_share"]), "Outer wall speed",
            "Slower outside walls come out smoother, with less ringing (as the slicers' own high-quality profiles)")
        if profile.printer.get("speed", "fast") == "fast":
            put("outer_wall_acceleration", looks["outer_wall_acceleration"], "Outer wall acceleration",
                "Gentler starts and stops on the outside walls: less ringing next to corners")
        put("wall_sequence", looks["wall_sequence"], "Wall order", "The outside wall prints between two inside ones, undisturbed:"
            " a cleaner surface and truer size (OrcaSlicer, Bambu Studio)")
        put("seam_position", looks["seam_position"], "Seam", "The seam lines up in one place instead of jumping from corner to corner")
    if stats.get("round_share", 0) >= T["seam"]["round_share"] and goal != "speed":
        put("seam_slope_type", "external", "Scarf seam", "Round outline with no corner to hide the seam in: the scarf seam"
            " blends the start and end of each loop instead of leaving a line of blobs (OrcaSlicer / Bambu Studio)")
    if rules & {"gap_fuses", "shrinkage_size_error"}:
        put("precise_outer_wall", 1, "Precise wall", "Keeps the outer wall from bulging outwards, so outside sizes and gaps come"
            f" out closer to the model ({part_fact(findings, 'gap_fuses', 'shrinkage_size_error')}; OrcaSlicer only, other slicers ignore it)",
            fixes=("gap_fuses", "shrinkage_size_error"))
    if rules & {"bridge_sag", "long_bridge", "sideways_hole_oval"}:
        put("bridge_speed", 25, "Bridge speed", f"Slower bridges sag less: {part_fact(findings, 'long_bridge', 'bridge_sag', 'sideways_hole_oval')}",
            fixes=("long_bridge", "bridge_sag", "sideways_hole_oval"))
    return out
