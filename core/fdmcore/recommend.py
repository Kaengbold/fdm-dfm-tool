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


def goal_plan(profile: Profile) -> dict:
    """Walls, infill (%), pattern, layer height and top/bottom layers for the user's goal."""
    plan = dict(GOAL_PLANS[profile.goal])
    lh = profile.layer_height * plan.pop("layer_factor")
    plan["layer_height"] = round(min(lh, 0.7 * float(profile.nozzle)) / 0.02) * 0.02
    plan["layer_height"] = round(plan["layer_height"], 2)
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

    if rules & {"small_bed_contact", "tipping_risk", "warping"}:
        width = T["warping"]["brim_width_mm"] if "warping" in rules or mat["warp_risk"] == "high" else 5
        put("brim_type", "outer_only", "Brim", "Holds the part down: small contact, tall part or warping")
        put("brim_width", width, "Brim width", "Wide enough to stop corners lifting")
    else:
        put("brim_type", "no_brim", "Brim", "Enough bed contact without one")

    if rules & {"too_thin_to_print", "fine_detail_rough", "single_line_wall"}:
        put("wall_generator", "arachne", "Wall generator", "Arachne varies line width so thin details still print")
    if "small_hole_undersized" in rules:
        put("xy_hole_compensation", T["holes"]["xy_hole_compensation_mm"], "Hole compensation", "Small holes print undersized; this opens them up")
    if rules & {"small_layers_cooling", "tall_thin_feature"}:
        put("slow_down_for_layer_cooling", 1, "Slow down small layers", "Gives small layers time to cool")
    if rules & {"elephant_foot", "first_layer_detail"}:
        put("elefant_foot_compensation", 0.15, "Elephant foot compensation", "Keeps the bottom edges and holes true to size")
    if rules & {"bridge_sag", "long_bridge", "sideways_hole_oval"}:
        put("bridge_speed", 25, "Bridge speed", "Slower bridges sag less")
    return out
