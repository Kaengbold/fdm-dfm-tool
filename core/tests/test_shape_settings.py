"""Shape measurements and setting decisions, independently pinned to simple known sections."""

import json

import numpy as np
import pytest
from shapely.geometry import MultiPolygon, Polygon, box

from fdmcore.checks import _wall_width_samples, shape_settings_stats
from fdmcore.profile import Profile, load_data
from fdmcore.recommend import goal_plan, recommend_settings
from fdmcore.slicer import Layer


def measured(shape, height=30, **profile):
    p = Profile.from_dict(profile)
    layers = [Layer(i, float(z), p.layer_height, shape) for i, z in enumerate(np.arange(0.1, height, p.layer_height))]
    stats = shape_settings_stats(layers, p, load_data("thresholds")["shape_settings"])
    stats["top_flat_mm2"] = shape.area
    settings = {s["key"]: s for s in recommend_settings(p, [], stats)}
    return p, stats, settings


def triple(settings):
    return tuple(settings[k]["value"] for k in ("wall_loops", "sparse_infill_density", "top_shell_layers"))


def test_thin_shell_fills_with_only_the_loops_that_fit():
    ring = box(0, 0, 40, 30).difference(box(0.8, 0.8, 39.2, 29.2))
    p, stats, settings = measured(ring)
    assert stats["wall_min_mm"] == pytest.approx(0.8)
    assert stats["wall_typical_mm"] == pytest.approx(0.8)
    assert stats["infill_volume_share"] == 0
    assert triple(settings) == ("1", "15%", "4")
    assert "fill them solid" in settings["wall_loops"]["reason"]
    assert "hardly used here" in settings["sparse_infill_density"]["reason"]
    assert "0.80 mm" in settings["wall_loops"]["reason"]
    assert int(settings["wall_loops"]["value"]) < goal_plan(p)["walls"]


def test_thick_block_keeps_walls_and_backs_its_large_interior():
    p, stats, settings = measured(box(0, 0, 40, 40))
    assert stats["wall_typical_mm"] == pytest.approx(40)
    assert stats["infill_volume_share"] == pytest.approx(0.82, abs=0.03)
    assert triple(settings) == ("3", "20%", "4")
    assert "cm³" in settings["wall_loops"]["reason"]
    assert "Balanced" in settings["sparse_infill_density"]["reason"]
    assert int(settings["sparse_infill_density"]["value"].rstrip("%")) > goal_plan(p)["infill"]


def test_mixed_part_keeps_walls_for_its_block_and_closes_top_over_low_infill():
    mixed = box(0, 0, 20, 20).union(box(20, 0, 60, 1.6))
    _, stats, settings = measured(mixed, height=15)
    assert stats["wall_min_mm"] == pytest.approx(1.6)
    assert 0.4 < stats["infill_volume_share"] < 0.8
    assert triple(settings) == ("3", "15%", "4")  # a small interior keeps the goal's infill
    assert settings["wall_generator"]["value"] == "arachne"
    assert "1.60 mm" in settings["wall_generator"]["reason"]


@pytest.mark.parametrize("goal", ("balanced", "strength", "speed", "material"))
def test_goal_remains_starting_point_and_low_infill_gets_extra_top(goal):
    """A small interior never gets less infill than the goal: it saves next to nothing and weakens
    hooks and brackets. Low infill from the goal itself still closes the top with one more layer."""
    p, _, settings = measured(box(0, 0, 8, 8), height=8, goal=goal)
    base = goal_plan(p)
    assert int(settings["wall_loops"]["value"]) == base["walls"]
    infill = int(settings["sparse_infill_density"]["value"].rstrip("%"))
    assert infill == base["infill"]
    assert int(settings["top_shell_layers"]["value"]) == base["top"] + (infill <= 10)
    assert settings["bottom_shell_layers"]["value"] == str(base["bottom"])


@pytest.mark.parametrize("nozzle", ("0.2", "0.4", "0.8"))
def test_non_integral_thin_walls_get_arachne_at_each_nozzle(nozzle):
    p = Profile.from_dict({"nozzle": nozzle})
    width = p.line_width * 2.5
    ring = box(0, 0, 20, 20).difference(box(width, width, 20 - width, 20 - width))
    _, _, settings = measured(ring, nozzle=nozzle)
    assert settings["wall_generator"]["value"] == "arachne"
    assert f"{width:.2f} mm" in settings["wall_generator"]["reason"]
    assert triple(settings) == ("2", "15%", str(goal_plan(p)["top"]))


def test_whole_line_walls_do_not_ask_for_arachne():
    width = Profile.from_dict({}).line_width * 3
    ring = box(0, 0, 20, 20).difference(box(width, width, 20 - width, 20 - width))
    _, _, settings = measured(ring)
    assert "wall_generator" not in settings


def test_widths_handle_disconnected_sections_and_holes():
    ring = box(0, 0, 20, 20).difference(box(1.6, 1.6, 18.4, 18.4))
    widths, _ = _wall_width_samples(MultiPolygon([ring, box(30, 0, 40, 2)]), 128)
    assert np.percentile(widths, 10) == pytest.approx(1.6)
    assert set(np.round(widths, 1)) >= {1.6, 2.0}


def test_empty_sections_have_no_measurements():
    assert shape_settings_stats([Layer(0, 0.1, 0.2, Polygon())], Profile.from_dict({}),
                                load_data("thresholds")["shape_settings"]) == {}


def test_low_infill_also_closes_tops_without_a_flat_top_measurement():
    p = Profile.from_dict({"goal": "speed"})
    settings = {s["key"]: s for s in recommend_settings(p, [])}
    assert settings["top_shell_layers"]["value"] == str(goal_plan(p)["top"] + 1)
    assert "10%" in settings["top_shell_layers"]["reason"]


def test_measurements_reuse_repeated_sections(monkeypatch):
    import fdmcore.checks as checks

    calls = []
    original = checks._wall_width_samples

    def sample(g, count):
        calls.append(g)
        return original(g, count)

    monkeypatch.setattr(checks, "_wall_width_samples", sample)
    _, stats, settings = measured(box(0, 0, 20, 20))
    assert len(calls) == 1
    json.dumps({"stats": stats, "settings": settings}, allow_nan=False)


def test_check_slices_once_and_estimates_the_recommended_plan(monkeypatch):
    import fdmcore.checks as checks
    from fdmcore.additions import extrude
    from fdmcore.mesh import Mesh
    from fdmcore.recommend import shape_plan

    mesh = Mesh(extrude(box(0, 0, 40, 40), 0, 30))
    p = Profile.from_dict({})
    slice_calls, estimate_plans = [], []
    original_slice, original_estimate = checks.slice_mesh, checks._estimate

    def sliced(*args):
        slice_calls.append(True)
        return original_slice(*args)

    def estimate(layers, profile, plan=None):
        estimate_plans.append(plan)
        return original_estimate(layers, profile, plan)

    monkeypatch.setattr(checks, "slice_mesh", sliced)
    monkeypatch.setattr(checks, "_estimate", estimate)
    result = checks.run_checks(mesh, p)
    expected, _ = shape_plan(p, result["stats"], result["findings"])
    assert len(slice_calls) == 1
    assert estimate_plans == [expected]
    assert expected["infill"] == 20
    assert result["stats"]["top_flat_mm2"] == 1600
    json.dumps(result, allow_nan=False)


def test_shape_driven_reasons_differ_with_measurements():
    a = measured(box(0, 0, 20, 20))[2]
    b = measured(box(0, 0, 40, 40))[2]
    for key in ("wall_loops", "sparse_infill_density"):
        assert a[key]["reason"] != b[key]["reason"]
    for settings in (a, b):
        assert all(s["reason"] for s in settings.values())
