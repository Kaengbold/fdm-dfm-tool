"""Layer decisions preserve detail and skin thickness; estimates count local fine layers."""

from pathlib import Path

import numpy as np
import pytest
from shapely.geometry import Polygon, box

from fdmcore.additions import extrude
from fdmcore.checks import _estimate, run_checks
from fdmcore.mesh import Mesh, load_stl
from fdmcore.profile import Profile
from fdmcore.recommend import goal_plan, recommend_settings, shape_plan
from fdmcore.slicer import slice_mesh

ROOT = Path(__file__).resolve().parents[2]


def roof(base=10):
    rise = 40 * np.tan(np.radians(10))
    return Mesh(extrude(Polygon([(0, 0), (40, 0), (40, base + rise), (0, base)]), 0, 40)[..., [0, 2, 1]][:, ::-1])


def checked(mesh, **profile):
    p = Profile.from_dict(profile)
    result = run_checks(mesh, p)
    plan, reasons = shape_plan(p, result["stats"], result["findings"])
    return p, result, plan, reasons


def test_detail_plate_and_sloped_part_differ_from_tray():
    _, detail, dp, _ = checked(load_stl((ROOT / "web/samples/detail_plate.stl").read_bytes()))
    p, tray, tp, reasons = checked(load_stl((ROOT / "core/tests/corpus/tray.stl").read_bytes()))
    _, slope, sp, why = checked(roof())
    assert dp["layer_height"] == sp["layer_height"] == 0.2
    assert tp["layer_height"] == 0.24
    assert "3.00 mm" in reasons["layer_height"]
    assert "thin layers only from 10 to 17.05 mm" in why["layer_height"]
    assert sp["fine_layers"][0]["layer_height"] == 0.08
    layers = slice_mesh(load_stl((ROOT / "core/tests/corpus/tray.stl").read_bytes()), p.layer_height, 160)
    usual = {**tp, "layer_height": goal_plan(p)["layer_height"]}
    assert tray["stats"]["print_time_min"] < _estimate(layers, p, usual)["print_time_min"]
    assert detail["findings"] and slope["findings"]


@pytest.mark.parametrize("goal", ["balanced", "strength", "speed", "material"])
def test_sustained_fine_detail_gets_finer_layers_without_losing_skin(goal):
    p, result, plan, reasons = checked(Mesh(extrude(box(0, 0, 30, 0.5), 0, 10)), goal=goal)
    usual = goal_plan(p)
    assert plan["layer_height"] == 0.16
    for key in ("top", "bottom"):
        assert plan[key] * plan["layer_height"] >= usual[key] * usual["layer_height"] - 1e-6
    assert "0.49 mm" in reasons["layer_height"] and "smaller nozzle" in reasons["layer_height"]
    assert "mm² of detail" in reasons["top"] and "mm² of detail" in reasons["bottom"]
    settings = {s["key"]: s for s in recommend_settings(p, result["findings"], result["stats"])}
    assert settings["layer_height"]["reason"] == reasons["layer_height"]


def test_full_height_slope_changes_base_height_and_retains_fine_ranges():
    p, result, plan, reasons = checked(roof(base=1))
    assert plan["layer_height"] == 0.12
    assert "mm²" in reasons["layer_height"] and "%" in reasons["layer_height"]
    layers = slice_mesh(roof(base=1), p.layer_height, 160)
    without_bands = {k: v for k, v in plan.items() if k != "fine_layers"}
    assert result["stats"]["print_time_min"] > _estimate(layers, p, without_bands)["print_time_min"]


def test_finer_layers_keep_the_extra_closing_layer_over_sparse_infill():
    p = Profile.from_dict({"goal": "speed"})
    stats = {"visible_surface_mm2": 1000, "height_mm": 8, "top_flat_mm2": 0}
    slope = {"rule": "shallow_slope_stairs", "metrics": {"area_mm2": 600, "z0": 1, "z1": 8, "layer_height": 0.08}}
    base, _ = shape_plan(p, {}, [])  # includes the extra solid top over 10% infill
    fine, reasons = shape_plan(p, stats, [slope])
    assert fine["layer_height"] == 0.12
    assert fine["top"] * fine["layer_height"] >= base["top"] * base["layer_height"] - 1e-6
    assert "extra solid top layer" in reasons["top"] and "600 mm²" in reasons["top"]


@pytest.mark.parametrize("goal,expected", [("balanced", 0.24), ("speed", 0.28), ("strength", 0.2), ("material", 0.2)])
def test_only_goals_that_allow_speed_get_thicker_plain_blocks(goal, expected):
    _, _, plan, _ = checked(Mesh(extrude(box(0, 0, 30, 30), 0, 10)), goal=goal)
    assert plan["layer_height"] == expected


def test_curves_and_even_tiny_detail_warnings_block_thicker_layers():
    p = Profile.from_dict({})
    stats = {"visible_surface_mm2": 1000, "plain_surface_share": 1, "wall_min_mm": 3, "height_mm": 20}
    for rule in ("first_layer_detail", "hole_closes", "fine_detail_rough", "side_detail_small"):
        plan, _ = shape_plan(p, stats, [{"rule": rule}])
        assert plan["layer_height"] == 0.2
    plan, _ = shape_plan(p, {**stats, "plain_surface_share": 0.9}, [])
    assert plan["layer_height"] == 0.2
    assert shape_plan(p, {}, [])[0]["layer_height"] == 0.2


def test_overlapping_height_ranges_count_once_and_keep_filament_volume():
    p = Profile.from_dict({})
    layers = slice_mesh(Mesh(extrude(box(0, 0, 2, 2), 0, 10)), p.layer_height, 160)
    base = {**goal_plan(p), "top": 100, "bottom": 100}
    band = {"z0": 2.05, "z1": 8.05, "layer_height": 0.08}
    plain = _estimate(layers, p, base)
    one = _estimate(layers, p, {**base, "fine_layers": [band]})
    two = _estimate(layers, p, {**base, "fine_layers": [band, band]})
    assert one == two
    assert one["print_time_min"] > plain["print_time_min"]
    assert one["filament_g"] == pytest.approx(plain["filament_g"])


@pytest.mark.parametrize("nozzle", ["0.2", "0.6", "0.8"])
def test_thick_layers_scale_safely_with_nozzle(nozzle):
    p, _, plan, _ = checked(Mesh(extrude(box(0, 0, 30, 30), 0, 10)), nozzle=nozzle)
    assert plan["layer_height"] <= 0.7 * float(nozzle)
    assert plan["layer_height"] >= goal_plan(p)["layer_height"]
