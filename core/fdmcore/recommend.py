"""Slicer settings recommendation from profile + findings.

Keys are OrcaSlicer / Bambu Studio setting names so they can go straight into a
3MF. Each recommendation carries a plain-language reason.
"""

from __future__ import annotations

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


def recommend_settings(profile: Profile, findings: list[dict], stats: dict | None = None) -> list[dict]:
    rules = {f["rule"] for f in findings}
    stats = stats or {}
    mat = profile.material
    T = load_data("thresholds")
    plan = goal_plan(profile)
    out: list[dict] = []

    def put(key, value, label, reason):
        out.append({"key": key, "value": str(value), "label": label, "reason": reason})

    goal = profile.goal
    put("layer_height", plan["layer_height"], "Layer height",
        "Thicker layers print faster" if goal == "speed" else f"Good default for a {profile.nozzle} mm nozzle")

    walls, infill = plan["walls"], plan["infill"]
    if rules & {"weak_upright_pin", "thin_neck", "single_line_wall"} and walls < 4 and goal not in ("speed", "material"):
        walls = 4
    put("wall_loops", walls, "Walls", {"strength": "Walls carry most of the strength", "speed": "Fewer walls print faster",
                                       "material": "Fewer walls save plastic"}.get(goal, "Good balance of strength and speed")
        + (" — raised for weak spots found" if walls > plan["walls"] else ""))
    put("sparse_infill_density", f"{infill}%", "Infill", {"strength": "Denser infill backs up the walls",
                                                          "speed": "Less infill prints faster",
                                                          "material": "Very little infill saves plastic"}.get(goal, "Matches your goal"))
    put("sparse_infill_pattern", plan["pattern"], "Infill pattern", PATTERN_REASON[plan["pattern"]])
    put("top_shell_layers", plan["top"], "Top layers", "More solid layers on top make it stiffer" if goal == "strength" else "Enough for a closed top surface")
    put("bottom_shell_layers", plan["bottom"], "Bottom layers", "More solid layers at the bottom make it stiffer" if goal == "strength" else "Enough for a closed bottom")

    if stats.get("needs_support") or "long_bridge" in rules:
        put("enable_support", 1, "Supports", "Some areas hang in the air in this orientation")
        put("support_type", "tree(auto)", "Support type", "Tree supports use less material and break off easily")
        # Orca's threshold is measured from the horizontal; ours from vertical.
        put("support_threshold_angle", round(90 - mat["max_overhang_deg"]), "Support threshold",
            f"{mat['name']} handles overhangs up to about {mat['max_overhang_deg']}° from vertical")
        if profile.multi_material:
            put("support_interface_pattern", "rectilinear_interlaced", "Support interface",
                "Dense interface; print it in a material that doesn't bond to the part (e.g. PETG for PLA) for clean undersides")
    else:
        put("enable_support", 0, "Supports", "Nothing needs support in this orientation")

    if rules & {"small_bed_contact", "tipping_risk"}:
        width = T["warping"]["brim_width_mm"] if "warping" in rules or mat["warp_risk"] == "high" else 5
        put("brim_type", "outer_only", "Brim", "Holds the part down: small contact or a tall part")
        put("brim_width", width, "Brim width", "Wide enough to stop corners lifting")
    elif "warping" in rules:
        # Only the corners lift: ears at the sharp corners hold them with far less brim to cut off
        # (OrcaSlicer wiki, Brim: Mouse ears). PrusaSlicer has none and gets an outer brim instead.
        put("brim_type", "brim_ears", "Brim", "Small ears at the corners, where warping starts; less to cut off than a full brim")
        put("brim_width", T["warping"]["brim_width_mm"], "Brim width", "Wide enough to stop corners lifting")
    else:
        put("brim_type", "no_brim", "Brim", "Enough bed contact without one")

    if rules & {"too_thin_to_print", "fine_detail_rough", "single_line_wall"}:
        put("wall_generator", "arachne", "Wall generator", "Arachne varies line width so thin details still print")
    # Measured on the user's own prints (My materials): corrects every hole / outline, measured beats the default.
    hole_err, outer_err = mat.get("hole_error_mm"), mat.get("outer_error_mm")
    if hole_err is not None and abs(hole_err) >= T["holes"]["measured_min_mm"]:
        put("xy_hole_compensation", round(-hole_err / 2, 3), "Hole compensation",
            f"Your prints make holes {abs(hole_err):.2f} mm {'smaller' if hole_err < 0 else 'bigger'} than modelled (you measured it): this corrects them")
    if outer_err is not None and abs(outer_err) >= T["holes"]["measured_min_mm"]:
        put("xy_contour_compensation", round(-outer_err / 2, 3), "Outer size compensation",
            f"Your prints come out {abs(outer_err):.2f} mm {'bigger' if outer_err > 0 else 'smaller'} outside than modelled (you measured it): this corrects it")
    if "small_hole_undersized" in rules and not any(s["key"] == "xy_hole_compensation" for s in out):
        put("xy_hole_compensation", T["holes"]["xy_hole_compensation_mm"], "Hole compensation", "Small holes print undersized; this opens them up")
        put("hole_to_polyhole", 1, "Polyholes", "Prints round holes as polygons with fewer, longer sides: they come out much closer to size (OrcaSlicer only: Bambu Studio and PrusaSlicer ignore it)")
    if rules & {"small_layers_cooling", "tall_thin_feature"}:
        put("slow_down_for_layer_cooling", 1, "Slow down small layers", "Gives small layers time to cool")
    # The first layer is squashed onto the bed and bulges out; pulling it in a little keeps bottom
    # edges straight. Most printer profiles ship 0.1-0.2 mm; harmless when the profile already has it.
    put("elefant_foot_compensation", 0.15, "Elephant foot compensation",
        "Keeps the bottom edges and holes at the bed true to size" if rules & {"elephant_foot", "first_layer_detail"}
        else "The first layer gets squashed and bulges out; this pulls it in so the bottom edge stays straight")
    if stats.get("top_flat_mm2", 0) >= T["ironing"]["min_top_area_mm2"] and goal in ("balanced", "strength"):
        put("ironing_type", "topmost", "Ironing", "The flat top comes out smooth: the nozzle passes over it once more with almost no"
            " plastic (adds a few minutes)")
    if stats.get("round_share", 0) >= T["seam"]["round_share"] and goal != "speed":
        put("seam_slope_type", "external", "Scarf seam", "Round outline with no corner to hide the seam in: the scarf seam"
            " blends the start and end of each loop instead of leaving a line of blobs (OrcaSlicer / Bambu Studio)")
    if rules & {"gap_fuses", "shrinkage_size_error"}:
        put("precise_outer_wall", 1, "Precise wall", "Keeps the outer wall from bulging outwards, so outside sizes and gaps come"
            " out closer to the model (OrcaSlicer; other slicers ignore it)")
    if rules & {"bridge_sag", "long_bridge", "sideways_hole_oval"}:
        put("bridge_speed", 25, "Bridge speed", "Slower bridges sag less")
    return out
