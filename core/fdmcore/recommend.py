"""Slicer settings recommendation from profile + findings.

Keys are OrcaSlicer / Bambu Studio setting names so they can go straight into a
3MF. Each recommendation carries a plain-language reason.
"""

from __future__ import annotations

from .profile import Profile


def recommend_settings(profile: Profile, findings: list[dict]) -> list[dict]:
    rules = {f["rule"] for f in findings}
    mat = profile.material
    out: list[dict] = []

    def put(key, value, label, reason):
        out.append({"key": key, "value": str(value), "label": label, "reason": reason})

    put("layer_height", profile.layer_height, "Layer height", f"Good default for a {profile.nozzle} mm nozzle")
    put("wall_loops", 3 if mat["warp_risk"] != "low" or "thin_wall" in rules else 2, "Walls",
        "Extra wall for strength" if mat["warp_risk"] != "low" else "Standard for most parts")
    put("sparse_infill_density", "15%", "Infill", "Enough for most parts; strength checks come in a later version")
    put("sparse_infill_pattern", "gyroid", "Infill pattern", "Strong in all directions and prints quietly")

    if "unsupported_overhang" in rules or "long_bridge" in rules:
        put("enable_support", 1, "Supports", "Some areas hang in the air in this orientation")
        put("support_type", "tree(auto)", "Support type", "Tree supports use less material and break off easily")
        # Orca's threshold is measured from the horizontal; ours from vertical.
        put("support_threshold_angle", round(90 - mat["max_overhang_deg"]), "Support threshold",
            f"{mat['name']} handles overhangs up to about {mat['max_overhang_deg']}° from vertical")
    else:
        put("enable_support", 0, "Supports", "Nothing needs support in this orientation")

    if rules & {"small_bed_contact", "tipping_risk", "warping_material"} or (
        "sharp_bed_corners" in rules and mat["warp_risk"] != "low"
    ):
        width = 8 if mat["warp_risk"] == "high" or "tipping_risk" in rules else 5
        put("brim_type", "outer_only", "Brim", "Holds the part down: small contact, sharp corners or warping material")
        put("brim_width", width, "Brim width", "Wider brim for warping materials and tall parts")
    else:
        put("brim_type", "no_brim", "Brim", "Enough bed contact without one")

    if rules & {"thin_wall", "too_thin_to_print"}:
        put("wall_generator", "arachne", "Wall generator", "Arachne varies line width to fill thin walls")
    if rules & {"small_layers_cooling", "tall_thin_feature"}:
        put("slow_down_for_layer_cooling", 1, "Slow down small layers", "Gives small layers time to cool")
    if "elephant_foot" in rules:
        put("elefant_foot_compensation", 0.15, "Elephant foot compensation", "Keeps holes on the bed from shrinking")
    return out
