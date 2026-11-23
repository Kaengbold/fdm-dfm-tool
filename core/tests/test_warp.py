import json

import numpy as np
import pytest
from shapely.geometry import box

from fdmcore import api, warp
from fdmcore.additions import extrude
from fdmcore.mesh import Mesh, to_binary_stl
from fdmcore.profile import Profile

ABS_OPEN = {"material": "abs", "printer": "prusa_mk4s"}


def bar(length, height=3.0):
    return Mesh(extrude(box(0, 0, length, 10), 0, height))


def test_longer_bars_curl_more_and_the_reference_bar_rates_one():
    p = Profile.from_dict(ABS_OPEN)
    ref = warp.reference(p, 1.0)
    curls = [warp.curl(warp.simulate(bar(n), [0, 0, 1], p, h=1.0)) for n in (20, 45, 90)]
    assert curls[0] < curls[1] < curls[2]
    assert np.sqrt(curls[1] / ref) == pytest.approx(1.0, abs=0.05)  # 45 mm is the ABS / open printer limit


CAL_PRINTERS = {
    "pei": {"based_on": "prusa_mk4s", "bed_surface": "textured_pei"},
    "glass": {"based_on": "prusa_mk4s", "bed_surface": "glass"},
    "enclosed": {"based_on": "bambu_p1s", "bed_surface": "textured_pei"},
}


@pytest.fixture
def abs_on_pei():
    api.custom_presets(printers=CAL_PRINTERS)
    try:
        yield {**ABS_OPEN, "printer": "my_pei"}
    finally:
        api.custom_presets()


def test_fit_grip_from_synthetic_bar_results(abs_on_pei):
    def outcomes(profile):
        p = Profile.from_dict(profile)
        return [warp.simulate(bar(n), [0, 0, 1], p, api._shells(p), h=1, adhesion=True)["adhesion"]["may_lift"]
                for n in (40, 80)]

    known_grip = warp.bed_grip(Profile.from_dict(abs_on_pei))["grip_n_mm"]
    assert outcomes(abs_on_pei) == [False, True]
    r = api.dispatch("calibrate_bed_grip", json.dumps({"longest_flat_mm": 40, "profile": abs_on_pei}))
    enc = r.pop("enclosure")
    assert known_grip < r["upper_n_mm"] and enc == "none"
    assert r["grip_n_mm"] == pytest.approx(known_grip, rel=0.15)

    api.custom_presets(CAL_PRINTERS, materials={"abs_test": {
        "based_on": "abs", "bed_temp_c": 105, "bed_grip_calibration": {enc: r}, "warp_length_mm": {enc: 62},
    }})
    measured = {**abs_on_pei, "material": "my_abs_test"}
    p = Profile.from_dict(measured)
    grip = warp.bed_grip(p)
    assert grip["shear_source"] == "calibrated" and grip["confidence"] == "medium"
    assert grip["grip_n_mm"] == r["grip_n_mm"]
    assert grip["calibration_temp_differs"] and grip["calibration_temp_c"] == 100
    assert outcomes(measured) == [False, True]
    assert warp.warp_limit(p) == 62
    for pid in ("my_glass", "my_enclosed", "prusa_mk4s"):
        q = Profile.from_dict({**measured, "printer": pid})
        assert warp.bed_grip(q)["shear_source"] != "calibrated"
    enclosed = Profile.from_dict({**measured, "printer": "my_enclosed"})
    assert warp.warp_limit(enclosed) == warp.warp_limit(Profile.from_dict({"material": "abs"}))


def test_fit_grip_bounds_and_outcomes_the_model_cannot_fit(abs_on_pei):
    all_flat = api.calibrate_bed_grip(160, abs_on_pei)
    assert all_flat["status"] == "lower_only" and all_flat["lower_n_mm"] > 0
    assert "grip_n_mm" not in all_flat and "upper_n_mm" not in all_flat
    none_flat = api.calibrate_bed_grip(0, {**abs_on_pei, "layer_height": 0.1})
    assert none_flat["status"] == "upper_only" and none_flat["upper_n_mm"] > 0
    assert "grip_n_mm" not in none_flat and "lower_n_mm" not in none_flat
    assert api.calibrate_bed_grip(0, abs_on_pei)["status"] == "incompatible"  # the fixed curl screen holds the 40 mm bar
    assert api.calibrate_bed_grip(80, abs_on_pei)["status"] == "incompatible"  # longer bar has a lower threshold
    assert api.calibrate_bed_grip(40, ABS_OPEN)["status"] == "unknown_plate"
    with pytest.raises(ValueError, match="length from the warping test"):
        api.calibrate_bed_grip(90, abs_on_pei)


def test_fit_grip_uses_geometric_middle_of_positive_bracket(abs_on_pei, monkeypatch):
    calls = []

    def simulate_bar(mesh, *_args, **kwargs):
        length = int(np.ptp(mesh.triangles[..., 0]))
        calls.append(length)
        assert kwargs["grip_trial"]
        return {"lift_grip_n_mm": {80: 0.04, 120: 0.09}[length], "peel_solved": True}

    monkeypatch.setattr(warp, "simulate", simulate_bar)
    r = api.calibrate_bed_grip(80, abs_on_pei)
    assert calls == [80, 120]
    assert r["status"] == "calibrated"
    assert r["lower_n_mm"] < r["grip_n_mm"] < r["upper_n_mm"]
    assert r["grip_n_mm"] == pytest.approx(0.06)


def test_bottom_layers_on_the_warm_bed_shrink_less():
    p = Profile.from_dict(ABS_OPEN)
    s = warp.shrink_while_printing(np.array([0.1, 1.0, 5.0, 20.0]), p)
    assert np.all(np.diff(s) >= 0) and s[0] < 0.3 * s[-1]


def test_enclosure_and_pla_warp_less():
    def ratio(prof):
        api.load(to_binary_stl(bar(90)), "bar")
        return api.warping([0, 0, 1], prof)["lift_ratio"]

    open_abs = ratio(ABS_OPEN)
    assert open_abs > 1.3
    assert ratio({"material": "abs", "printer": "bambu_x1c"}) < open_abs
    assert ratio({"material": "pla", "printer": "prusa_mk4s"}) < open_abs


def test_warping_result_for_the_view():
    api.load(to_binary_stl(bar(90)), "bar")
    r = api.warping([0, 0, 1], ABS_OPEN)
    tris = np.frombuffer(r["field_triangles"], np.float32).reshape(-1, 3, 3)
    assert len(np.frombuffer(r["vertex_disp"], np.float32)) == 3 * len(np.frombuffer(r["vertex_value"], np.float32)) == 9 * len(tris)
    assert r["verdict"] == "likely to lift" and r["lift_points_print"]
    ends = [abs(p[0]) for p in r["lift_points_print"]]
    assert max(ends) > 35  # the bar's ends (it is centred on the bed origin), not its middle
    assert "brim" in r["explanation"]


@pytest.mark.parametrize("h", [1.0, None])
def test_open_abs_short_bar_holds_and_long_bar_peels(h):
    p = Profile.from_dict(ABS_OPEN)
    short, long = (warp.simulate(bar(n), [0, 0, 1], p, h=h, adhesion=True)["adhesion"] for n in (40, 90))
    assert not short.get("may_lift")
    assert long["may_lift"] and long["advance_mm"] >= 2
    assert long["energy_release_n_mm"] > long["grip_n_mm"] > 0
    assert min(abs(long["point"][0] - x) for x in (0, 90)) < 2


def test_peel_finding_is_local_and_contributes_to_print_risk():
    api.custom_presets(printers={"my_glass": {"based_on": "prusa_mk4s", "bed_surface": "glass"}})
    try:
        api.load(to_binary_stl(bar(90)), "bar")
        up = [0, 0, 1]
        assert not any(f["rule"] == "bed_adhesion" for f in api.check(up, ABS_OPEN)["findings"])
        r = api.warping(up, ABS_OPEN)
        assert r["adhesion"]["assumed_surface"] and r["adhesion"]["surface"] == "textured_pei"
        check = api.check(up, ABS_OPEN)
        finding = next(f for f in check["findings"] if f["rule"] == "bed_adhesion")
        assert finding["title"] == "May come loose here"
        assert finding["bbox"][0][2] == 0 and abs(finding["bbox"][0][0]) > 35
        assert any("build plate" in reason for reason in check["print_risk"]["reasons"])
        changed_plate = api.check(up, {**ABS_OPEN, "printer": "my_glass"})
        assert not any(f["rule"] == "bed_adhesion" for f in changed_plate["findings"])
    finally:
        api.custom_presets()


def test_expert_grip_override_can_stop_peel():
    path = "thresholds.bed_adhesion.nominal_shear_n_mm.abs.textured_pei"
    try:
        api.set_expert({path: 10})
        p = Profile.from_dict(ABS_OPEN)
        r = warp.simulate(bar(90), [0, 0, 1], p, h=1, adhesion=True)["adhesion"]
        assert r["curls"] and r["advance_mm"] == 0 and not r["may_lift"]
        assert r["energy_release_n_mm"] < r["grip_n_mm"]
    finally:
        api.set_expert({})


def test_pla_grip_uses_the_anchor_for_the_plate_and_bed_temperature():
    api.custom_presets(
        printers={s: {"based_on": "prusa_mk4s", "bed_surface": s} for s in ("glass", "polyimide")},
        materials={str(t): {"based_on": "pla", "bed_temp_c": t} for t in (60, 70)},
    )
    try:
        for plate, temp, shear in (("glass", 60, 0.73), ("glass", 70, 6.51),
                                   ("polyimide", 60, 0.51), ("polyimide", 70, 3.22)):
            grip = warp.bed_grip(Profile.from_dict({"material": f"my_{temp}", "printer": f"my_{plate}"}))
            assert grip["shear_source"] == "measured shear anchor" and grip["confidence"] == "low"
            assert grip["shear_n_mm"] == shear
            assert grip["grip_n_mm"] == pytest.approx(shear * 0.30 / (2 * 1.8))
    finally:
        api.custom_presets()


def test_thick_sections_stay_warm_higher_up():
    p = Profile.from_dict(ABS_OPEN)
    assert warp.fade_length(p, 5.0) > 2 * warp.fade_length(p, 1.0)
    thin, thick = (warp.simulate(Mesh(extrude(box(0, 0, 40, w), 0, 10)), [0, 0, 1], p, h=1.0) for w in (2, 20))
    zc = lambda r: r["model"].grid.centers(r["model"].elems)[:, 2]
    at = lambda r, z: r["shrink"][np.abs(zc(r) - z) < 0.6].mean()
    assert at(thick, 3.0) < at(thin, 3.0)  # 3 mm up: the thin wall has cooled, the block is still warm


def test_residual_stress_in_the_result():
    api.load(to_binary_stl(bar(90)), "bar")
    r = api.warping([0, 0, 1], ABS_OPEN)
    stress = np.frombuffer(r["stress_value"], np.float32)
    assert len(stress) == len(np.frombuffer(r["vertex_value"], np.float32))
    assert 0 < r["stress_max"] < 100 and r["stress_share"] > 0
    assert "stress" in r["explanation"]


def test_tilted_part_curls_like_the_flat_one():
    """A bar stored tilted but printed flat (``up`` along its own bottom normal) must warp like the
    flat bar: voxelised in the print frame it sits on the bed, not on one stair-step corner."""
    p = Profile.from_dict(ABS_OPEN)
    flat = warp.curl(warp.simulate(bar(45), [0, 0, 1], p, h=1.0))
    a = np.radians(30)
    turn = np.array([[np.cos(a), 0, np.sin(a)], [0, 1, 0], [-np.sin(a), 0, np.cos(a)]])
    tilted = Mesh(bar(45).triangles @ turn.T)
    assert warp.curl(warp.simulate(tilted, turn @ [0, 0, 1], p, h=1.0)) == pytest.approx(flat, rel=0.25)


def test_abs_curls_more_than_pla():
    """T-362: the same bar curls several times more in ABS than in PLA (shrinkage 0.8 vs 0.3 %); the view
    shows it true size on one scale, so the difference must be in the numbers."""
    from test_hold import box

    api.load(box(120, 20, 5), "bar")
    pla, abs_ = (api.warping([0, 0, 1], {"material": m}) for m in ("pla", "abs"))
    assert abs_["max_mm"] > 2.5 * pla["max_mm"]
    assert "curl up to" in abs_["explanation"] and abs_["verdict"] != "fine"
