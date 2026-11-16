"""Setting reasons name the part (T-426): a setting the part asked for says which fact of this part led
to it, so two parts with different facts never share a reason; a value from the goal alone says so."""

from fdmcore.profile import Profile
from fdmcore.recommend import part_fact, recommend_settings

# Two parts with the same problems but different measurements, as checks.py words its details.
PART_A = (
    [{"rule": "weak_upright_pin", "detail": "Pin/post ~3.2 mm wide stands 20 mm upright — the layers run across it, so it snaps easily"},
     {"rule": "small_bed_contact", "detail": "Only 35 mm² touches the bed (largest layer 400 mm²)"},
     {"rule": "too_thin_to_print", "detail": "About 0.25 mm thin at 4.0–6.0 mm — too thin for a 0.4 mm nozzle, it won't print"},
     {"rule": "small_hole_undersized", "detail": "Ø3.0 mm hole prints about 0.15–0.3 mm too small — a screw won't fit"},
     {"rule": "tall_thin_feature", "detail": "Slender feature ~2.0 mm wide and 30 mm tall wobbles while printing"},
     {"rule": "gap_fuses", "detail": "Gap about 0.15 mm at 2.0–8.0 mm — narrower than 0.2 mm fuses shut"},
     {"rule": "long_bridge", "detail": "25 mm bridge at 10.0 mm will sag (comfortable for PLA: ≤ 20 mm)"}],
    {"needs_support": True, "support_area_mm2": 1287.4, "top_flat_mm2": 900},
)
PART_B = (
    [{"rule": "weak_upright_pin", "detail": "Pin/post ~4.5 mm wide stands 32 mm upright — the layers run across it, so it snaps easily"},
     {"rule": "small_bed_contact", "detail": "Only 18 mm² touches the bed (largest layer 250 mm²)"},
     {"rule": "too_thin_to_print", "detail": "About 0.30 mm thin at 1.0–2.0 mm — too thin for a 0.4 mm nozzle, it won't print"},
     {"rule": "small_hole_undersized", "detail": "Ø5.0 mm hole prints about 0.15–0.3 mm too small — a screw won't fit"},
     {"rule": "tall_thin_feature", "detail": "Slender feature ~3.0 mm wide and 45 mm tall wobbles while printing"},
     {"rule": "gap_fuses", "detail": "Gap about 0.10 mm at 5.0–9.0 mm — narrower than 0.2 mm fuses shut"},
     {"rule": "long_bridge", "detail": "40 mm bridge at 22.0 mm will sag (comfortable for PLA: ≤ 20 mm)"}],
    {"needs_support": True, "support_area_mm2": 310.0, "top_flat_mm2": 1500},
)
# Settings these findings and stats switch on or change: their reason must come from the part.
PART_DRIVEN = {"wall_loops", "enable_support", "brim_type", "wall_generator", "xy_hole_compensation",
               "slow_down_for_layer_cooling", "precise_outer_wall", "bridge_speed", "ironing_type"}


def reasons(part, goal="balanced"):
    findings, stats = part
    return {s["key"]: s["reason"] for s in recommend_settings(Profile.from_dict({"goal": goal}), findings, stats)}


def test_part_driven_reasons_differ_between_parts():
    a, b = reasons(PART_A), reasons(PART_B)
    assert PART_DRIVEN <= a.keys() and PART_DRIVEN <= b.keys(), PART_DRIVEN - a.keys()
    same = {k: a[k] for k in PART_DRIVEN if a[k] == b[k]}
    assert not same, f"reasons that don't name the part: {same}"


def test_reasons_quote_the_measured_fact():
    a = reasons(PART_A)
    assert a["wall_loops"] == "Raised to 4 for a weak spot: pin/post ~3.2 mm wide stands 20 mm upright"
    assert a["enable_support"] == "1,287 mm² hang in the air in this orientation"
    assert a["brim_type"] == "Holds the part down: only 35 mm² touches the bed"
    assert "Ø3.0 mm hole prints about 0.15–0.3 mm too small" in a["xy_hole_compensation"]


def test_goal_only_values_say_so():
    plain = reasons(([], {}))
    assert plain["sparse_infill_density"] == "From your goal Balanced; nothing on this part asks for more"
    assert plain["wall_loops"] == plain["sparse_infill_density"]
    assert "From your goal Balanced" in plain["layer_height"]
    assert plain["enable_support"] == "Nothing hangs in the air in this orientation"


def test_part_fact_without_detail_is_empty():
    assert part_fact([{"rule": "gap_fuses"}], "gap_fuses") == ""
    assert part_fact([], "gap_fuses") == ""
