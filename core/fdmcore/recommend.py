"""Slicer settings recommendation from profile + findings.

Keys are OrcaSlicer / Bambu Studio setting names so they can go straight into a
3MF. Each recommendation carries a plain-language reason.
"""

from __future__ import annotations

from bisect import bisect_right
from math import ceil

from .profile import Profile, load_data

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
            reasons["layer_height"] = (f"This part has {area:,.0f} mm² of flat or upright surface; its thinner walls measure about {stats['wall_min_mm']:.2f} mm. "
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


def recommend_settings(profile: Profile, findings: list[dict], stats: dict | None = None) -> list[dict]:
    rules = {f["rule"] for f in findings}
    stats = stats or {}
    mat = profile.material
    T = load_data("thresholds")
    plan, shape_reasons = shape_plan(profile, stats, findings)
    out: list[dict] = []

    def put(key, value, label, reason):
        out.append({"key": key, "value": str(value), "label": label, "reason": reason})

    goal = profile.goal
    # Values that come from the goal alone say so; values this part asked for name its measured fact.
    from_goal = f"From your goal {GOAL_NAMES.get(goal, goal)}; nothing on this part asks for more"
    put("layer_height", plan["layer_height"], "Layer height",
        shape_reasons.get("layer_height", f"From your goal {GOAL_NAMES.get(goal, goal)} for a {profile.nozzle} mm nozzle; nothing on this part asks for finer layers"))

    walls, infill = plan["walls"], plan["infill"]
    put("wall_loops", walls, "Walls", shape_reasons.get("walls", from_goal))
    put("sparse_infill_density", f"{infill}%", "Infill", shape_reasons.get("infill", from_goal))
    put("sparse_infill_pattern", plan["pattern"], "Infill pattern", PATTERN_REASON[plan["pattern"]])
    put("top_shell_layers", plan["top"], "Top layers", shape_reasons.get("top", from_goal))
    put("bottom_shell_layers", plan["bottom"], "Bottom layers", shape_reasons.get("bottom", from_goal))

    overhang = stats.get("support_area_mm2", 0)
    if stats.get("needs_support") or "long_bridge" in rules:
        put("enable_support", 1, "Supports", f"{overhang:,.0f} mm² hang in the air in this orientation" if overhang >= 1
            else f"Holds up a long bridge: {part_fact(findings, 'long_bridge')}")
        put("support_type", "tree(auto)", "Support type", "Tree supports use less material and break off easily")
        # Orca's threshold is measured from the horizontal; ours from vertical.
        put("support_threshold_angle", round(90 - mat["max_overhang_deg"]), "Support threshold",
            f"{mat['name']} handles overhangs up to about {mat['max_overhang_deg']}° from vertical")
        if profile.multi_material:
            put("support_interface_pattern", "rectilinear_interlaced", "Support interface",
                "Dense interface; print it in a material that doesn't bond to the part (e.g. PETG for PLA) for clean undersides")
    else:
        put("enable_support", 0, "Supports", f"Only {overhang:,.0f} mm² overhang, little enough to print without" if overhang >= 1
            else "Nothing hangs in the air in this orientation")

    if rules & {"small_bed_contact", "tipping_risk"}:
        width = T["warping"]["brim_width_mm"] if "warping" in rules or mat["warp_risk"] == "high" else 5
        put("brim_type", "outer_only", "Brim", f"Holds the part down: {part_fact(findings, 'small_bed_contact', 'tipping_risk')}")
        put("brim_width", width, "Brim width", "Wide enough to stop corners lifting")
    elif "warping" in rules:
        # Only the corners lift: ears at the sharp corners hold them with far less brim to cut off
        # (OrcaSlicer wiki, Brim: Mouse ears). PrusaSlicer has none and gets an outer brim instead.
        put("brim_type", "brim_ears", "Brim", f"Ears at the corners, where warping starts ({part_fact(findings, 'warping')});"
            " less to cut off than a full brim")
        put("brim_width", T["warping"]["brim_width_mm"], "Brim width", "Wide enough to stop corners lifting")
    else:
        put("brim_type", "no_brim", "Brim", "Enough bed contact without one")

    if stats.get("uneven_wall_share", 0) >= T["shape_settings"]["uneven_wall_share"]:
        put("wall_generator", "arachne", "Wall generator",
            f"About {stats['uneven_wall_share']:.0%} of this part's wall samples, around {stats['uneven_wall_mm']:.2f} mm wide, "
            f"fall between whole {profile.line_width:.2f} mm lines; Arachne varies line width to fill them")
    elif rules & {"too_thin_to_print", "fine_detail_rough", "single_line_wall"}:
        put("wall_generator", "arachne", "Wall generator", "Arachne varies line width so thin details still print: "
            + part_fact(findings, "too_thin_to_print", "fine_detail_rough", "single_line_wall"))
    # Measured on the user's own prints (My materials): corrects every hole / outline, measured beats the default.
    hole_err, outer_err = mat.get("hole_error_mm"), mat.get("outer_error_mm")
    if hole_err is not None and abs(hole_err) >= T["holes"]["measured_min_mm"]:
        put("xy_hole_compensation", round(-hole_err / 2, 3), "Hole compensation",
            f"Your prints make holes {abs(hole_err):.2f} mm {'smaller' if hole_err < 0 else 'bigger'} than modelled (you measured it): this corrects them")
    if outer_err is not None and abs(outer_err) >= T["holes"]["measured_min_mm"]:
        put("xy_contour_compensation", round(-outer_err / 2, 3), "Outer size compensation",
            f"Your prints come out {abs(outer_err):.2f} mm {'bigger' if outer_err > 0 else 'smaller'} outside than modelled (you measured it): this corrects it")
    if "small_hole_undersized" in rules and not any(s["key"] == "xy_hole_compensation" for s in out):
        put("xy_hole_compensation", T["holes"]["xy_hole_compensation_mm"], "Hole compensation",
            f"Opens up small holes: {part_fact(findings, 'small_hole_undersized')}")
        put("hole_to_polyhole", 1, "Polyholes", "Prints round holes as polygons with fewer, longer sides: they come out much closer to size (OrcaSlicer only: Bambu Studio and PrusaSlicer ignore it)")
    if rules & {"small_layers_cooling", "tall_thin_feature"}:
        put("slow_down_for_layer_cooling", 1, "Slow down small layers",
            f"Gives small layers time to cool: {part_fact(findings, 'small_layers_cooling', 'tall_thin_feature')}")
    # The first layer is squashed onto the bed and bulges out; pulling it in a little keeps bottom
    # edges straight. Most printer profiles ship 0.1-0.2 mm; harmless when the profile already has it.
    put("elefant_foot_compensation", 0.15, "Elephant foot compensation",
        "Keeps the bottom edges and holes at the bed true to size" if rules & {"elephant_foot", "first_layer_detail"}
        else "The first layer gets squashed and bulges out; this pulls it in so the bottom edge stays straight")
    if stats.get("top_flat_mm2", 0) >= T["ironing"]["min_top_area_mm2"] and goal in ("balanced", "strength"):
        put("ironing_type", "topmost", "Ironing", f"The {stats['top_flat_mm2']:,.0f} mm² flat top comes out smooth: the nozzle passes"
            " over it once more with almost no plastic (adds a few minutes)")
    if stats.get("round_share", 0) >= T["seam"]["round_share"] and goal != "speed":
        put("seam_slope_type", "external", "Scarf seam", "Round outline with no corner to hide the seam in: the scarf seam"
            " blends the start and end of each loop instead of leaving a line of blobs (OrcaSlicer / Bambu Studio)")
    if rules & {"gap_fuses", "shrinkage_size_error"}:
        put("precise_outer_wall", 1, "Precise wall", "Keeps the outer wall from bulging outwards, so outside sizes and gaps come"
            f" out closer to the model ({part_fact(findings, 'gap_fuses', 'shrinkage_size_error')}; OrcaSlicer only, other slicers ignore it)")
    if rules & {"bridge_sag", "long_bridge", "sideways_hole_oval"}:
        put("bridge_speed", 25, "Bridge speed", f"Slower bridges sag less: {part_fact(findings, 'long_bridge', 'bridge_sag', 'sideways_hole_oval')}")
    return out
