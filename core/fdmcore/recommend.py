"""Slicer settings recommendation from profile + findings.

Keys are OrcaSlicer / Bambu Studio setting names so they can go straight into a
3MF. Each recommendation carries a plain-language reason.
"""

from __future__ import annotations

from .profile import Profile, load_data


def recommend_settings(profile: Profile, findings: list[dict], stats: dict | None = None) -> list[dict]:
    rules = {f["rule"] for f in findings}
    stats = stats or {}
    mat = profile.material
    T = load_data("thresholds")
    out: list[dict] = []

    def put(key, value, label, reason):
        out.append({"key": key, "value": str(value), "label": label, "reason": reason})

    put("layer_height", profile.layer_height, "Layer height", f"Good default for a {profile.nozzle} mm nozzle")

    # Goal decides walls/infill: walls carry most of the strength (2–3× more than infill).
    walls, infill = {"strength": (4, "25%"), "speed": (2, "10%"), "material": (2, "10%")}.get(profile.goal, (3, "15%"))
    if rules & {"weak_upright_pin", "thin_neck", "single_line_wall"} and walls < 4:
        walls = 4
    put("wall_loops", walls, "Walls", {"strength": "Walls carry most of the strength", "speed": "Fewer walls print faster",
                                       "material": "Fewer walls save plastic"}.get(profile.goal, "Good balance of strength and speed")
        + (" — raised for weak spots found" if walls == 4 and profile.goal != "strength" else ""))
    put("sparse_infill_density", infill, "Infill", "Matches your goal")
    put("sparse_infill_pattern", "gyroid", "Infill pattern", "Strong in all directions and prints quietly")

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
    if "bridge_sag" in rules or "long_bridge" in rules:
        put("bridge_speed", 25, "Bridge speed", "Slower bridges sag less")
    return out
