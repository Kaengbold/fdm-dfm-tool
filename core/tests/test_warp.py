import io
import json
import zipfile
from xml.etree import ElementTree as ET

import numpy as np
import pytest
from scipy import sparse
from shapely.geometry import box

from fdmcore import api, gcode, thermal, warp
from fdmcore.additions import extrude
from fdmcore.fea import cohesive_bed
from fdmcore.mesh import Mesh, to_binary_stl
from fdmcore.profile import Profile

ABS_OPEN = {"material": "abs", "printer": "prusa_mk4s"}


def bar(length, height=3.0):
    return Mesh(extrude(box(0, 0, length, 10), 0, height))


def bar_adhesion(length, profile):
    api.load(to_binary_stl(bar(length)), "bar")
    return api.warping([0, 0, 1], profile, settings={"brim_type": "no_brim"})["adhesion"]


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
        return [bar_adhesion(n, profile)["may_lift"] for n in (40, 80)]

    known_grip = warp.bed_grip(Profile.from_dict(abs_on_pei))["grip_n_mm"]
    assert outcomes(abs_on_pei) == [False, True]
    r = api.dispatch("calibrate_bed_grip", json.dumps({"longest_flat_mm": 40, "profile": abs_on_pei}))
    enc = r.pop("enclosure")
    assert r["lower_n_mm"] < known_grip < r["upper_n_mm"] and enc == "none"

    api.custom_presets(CAL_PRINTERS, materials={"abs_test": {
        "based_on": "abs", "bed_temp_c": 105, "bed_grip_calibration": {enc: r}, "warp_length_mm": {enc: 62},
    }})
    measured = {**abs_on_pei, "material": "my_abs_test"}
    p = Profile.from_dict(measured)
    grip = warp.bed_grip(p)
    assert grip["shear_source"] == "calibrated" and grip["confidence"] == "medium"
    curve = [warp.bed_grip(p, t, calibrated=False)["grip_n_mm"] for t in (105, 100)]
    assert grip["grip_n_mm"] == pytest.approx(r["grip_n_mm"] * curve[0] / curve[1])  # measured at 100 °C, used at 105 °C
    assert grip["calibration_temp_differs"] and grip["calibration_temp_c"] == 100
    assert outcomes(measured) == [False, True]
    assert warp.warp_limit(p) == 62
    for pid in ("my_glass", "my_enclosed", "prusa_mk4s"):
        q = Profile.from_dict({**measured, "printer": pid})
        assert warp.bed_grip(q)["shear_source"] != "calibrated"
    enclosed = Profile.from_dict({**measured, "printer": "my_enclosed"})
    assert warp.warp_limit(enclosed) == warp.warp_limit(Profile.from_dict({"material": "abs"}))


def test_fit_grip_one_sided_results_and_unknown_plate(abs_on_pei):
    all_flat = api.calibrate_bed_grip(160, abs_on_pei)
    assert all_flat["status"] == "lower_only" and all_flat["lower_n_mm"] > 0
    assert "grip_n_mm" not in all_flat and "upper_n_mm" not in all_flat
    none_flat = api.calibrate_bed_grip(0, abs_on_pei)
    assert none_flat["status"] == "upper_only" and none_flat["upper_n_mm"] > 0
    assert "grip_n_mm" not in none_flat and "lower_n_mm" not in none_flat
    assert api.calibrate_bed_grip(40, ABS_OPEN)["status"] == "unknown_plate"
    with pytest.raises(ValueError, match="length from the warping test"):
        api.calibrate_bed_grip(90, abs_on_pei)


def test_fit_grip_when_80_mm_holds_and_120_mm_lifts(abs_on_pei):
    r = api.calibrate_bed_grip(80, abs_on_pei)
    assert r["status"] == "calibrated"
    assert r["upper_n_mm"] > 1.15 * r["lower_n_mm"]
    assert r["lower_n_mm"] < r["grip_n_mm"] < r["upper_n_mm"]
    api.custom_presets(CAL_PRINTERS, materials={"long_bars": {
        "based_on": "abs", "bed_grip_calibration": {r["enclosure"]: r},
    }})
    results = [bar_adhesion(n, {**abs_on_pei, "material": "my_long_bars"}) for n in (80, 120)]
    assert all(r["shear_source"] == "calibrated" and not r["solver_failed"] for r in results)
    assert [r["may_lift"] for r in results] == [False, True]


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


def test_warp_shrink_follows_the_chamber_during_the_sliced_print():
    profile = Profile.from_dict({"material": "abs", "printer": "bambu_h2d"})
    layers = "M83\nG1 Z0.2\nG1 X10 E1\nG4 S1800\nG1 Z2.2\nG1 X0 E1\n"
    cold, warm = [gcode.parse(f"{command} S65\n" + layers) for command in ("M141", "M191")]

    def shrink(tp):
        result = warp.simulate(bar(10), [0, 0, 1], profile, h=1,
                               climate=thermal.chamber_conditions(profile, toolpath=tp), sliced=gcode.layer_times(tp))
        return result["shrink"].reshape(-1, 3).mean(axis=0)

    extra_shrink = shrink(cold) - shrink(warm)
    assert extra_shrink[0] > extra_shrink[-1] > 0


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
    r = api.dispatch("warping", json.dumps({"up": [0, 0, 1], "profile": ABS_OPEN}))
    tris = np.frombuffer(r["field_triangles"], np.float32).reshape(-1, 3, 3)
    assert len(np.frombuffer(r["vertex_disp"], np.float32)) == 3 * len(np.frombuffer(r["vertex_value"], np.float32)) == 9 * len(tris)
    assert r["verdict"] == "likely to lift" and r["lift_points_print"]
    ends = [abs(p[0]) for p in r["lift_points_print"]]
    assert max(ends) > 35  # the bar's ends (it is centred on the bed origin), not its middle
    assert "brim" in r["explanation"]
    low, high = r["range"]["curl_mm"]
    assert low < r["curl_mm"] < high
    low, high = r["adhesion"]["range"]
    assert low <= r["adhesion"]["may_lift"] <= high
    json.dumps(r, allow_nan=False, default=bytes.hex)


def test_removing_the_brim_preserves_the_bare_part_solve():
    p = Profile.from_dict(ABS_OPEN)
    mesh = bar(20)
    bare = warp.simulate(mesh, [0, 0, 1], p, h=1, adhesion=True)
    off = warp.simulate(mesh, [0, 0, 1], p, h=1, adhesion=True, brim={"brim_type": "no_brim", "brim_width": 8})
    np.testing.assert_array_equal(off["model"].grid.solid, bare["model"].grid.solid)
    np.testing.assert_array_equal(off["u"], bare["u"])
    assert warp.curl(off) == warp.curl(bare)
    assert off["adhesion"] == bare["adhesion"]


@pytest.mark.parametrize("kind", ["outer_only", "brim_ears"])
def test_first_layer_brim_holds_a_bar_that_lifts_without_it(kind):
    p = Profile.from_dict(ABS_OPEN)
    mesh = bar(90)
    bare = warp.simulate(mesh, [0, 0, 1], p, h=1, adhesion=True)
    brim = warp.simulate(mesh, [0, 0, 1], p, h=1, adhesion=True, brim={"brim_type": kind, "brim_width": 8})
    assert bare["adhesion"]["may_lift"]
    assert not brim["adhesion"]["solver_failed"]
    assert not brim["adhesion"]["may_lift"] or brim["adhesion"]["brim_lifts"]
    assert brim["brim_modeled"]
    added = brim["model"].grid.centers(brim["model"].elems[~brim["part_elems"]])
    assert np.all(added[:, 2] == added[0, 2])
    assert len(brim["model"].elems) <= 6000
    assert warp.curl(brim) < warp.curl(bare)
    if kind == "brim_ears":
        assert np.all(np.minimum(abs(added[:, 0]), abs(90 - added[:, 0])) < 8)
        assert all(np.any(np.linalg.norm(added[:, :2] - corner, axis=1) < 2)
                   for corner in ((0, 0), (0, 10), (90, 0), (90, 10)))


def test_warping_exports_brim_settings_and_displays_only_the_part():
    mesh = bar(60)
    api.load(to_binary_stl(mesh), "bar")
    settings = {"brim_type": "brim_ears", "brim_width": "8"}
    r = api.dispatch("warping", json.dumps({"up": [0, 0, 1], "profile": ABS_OPEN, "ranges": False,
                                          "settings": settings, "slicer": "prusa"}))
    with zipfile.ZipFile(io.BytesIO(api.export([0, 0, 1], ABS_OPEN, settings, slicer="prusa"))) as archive:
        config = ET.fromstring(archive.read("Metadata/Slic3r_PE_model.config"))
    exported = {m.attrib["key"]: m.attrib["value"] for m in config.iter("metadata")}
    assert r["brim"]["type"] == exported["brim_type"] == "outer_only"
    assert r["brim"]["width_mm"] == float(exported["brim_width"]) and r["brim"]["modeled"]
    tris = np.frombuffer(r["field_triangles"], np.float32).reshape(-1, 3)
    for actual, expected in zip((tris.min(axis=0), tris.max(axis=0)), mesh.bounds()):
        np.testing.assert_array_equal(actual, expected)
    assert len(np.frombuffer(r["vertex_disp"], np.float32)) == tris.size
    assert len(np.frombuffer(r["stress_value"], np.float32)) == len(tris)
    assert len(api._require().warp["model"].elems) < 6000
    if "point_print" in r["adhesion"] and not r["adhesion"].get("brim_lifts"):
        assert abs(r["adhesion"]["point_print"][0]) < 31
    json.dumps(r, allow_nan=False, default=bytes.hex)


def test_a_brim_smaller_than_a_grid_cell_falls_back_to_the_bare_part():
    p = Profile.from_dict(ABS_OPEN)
    r = warp.simulate(bar(20), [0, 0, 1], p, h=1, brim={"brim_type": "outer_only", "brim_width": 0.01})
    assert r["brim_modeled"] is False
    assert "part_model" not in r


def test_first_lift_uses_the_brim_only_when_the_part_stays_down(monkeypatch):
    p = Profile.from_dict(ABS_OPEN)
    r = warp.simulate(bar(20), [0, 0, 1], p, h=1, brim={"brim_type": "outer_only", "brim_width": 2})
    part_bed = r["part_nodes"][r["bed"]]
    neighboring_brim_nodes = np.flatnonzero(~part_bed)
    part_nodes = np.flatnonzero(part_bed)
    assert len(neighboring_brim_nodes) and len(part_nodes)

    lift = np.zeros(len(part_bed))
    lift[neighboring_brim_nodes[0]] = 0.1
    monkeypatch.setattr(warp, "cohesive_bed", lambda *args, **kwargs: (lift, lift, True))
    load = np.zeros((len(lift), 3))
    result = warp._edge_peel(r["model"], r["c"], r["scale"], load, 1, 1, part_bed=part_bed)
    assert result["brim_lifts"]
    lift[part_nodes[0]] = 0.05
    result = warp._edge_peel(r["model"], r["c"], r["scale"], load, 1, 1, part_bed=part_bed)
    assert not result["brim_lifts"]


@pytest.mark.parametrize("h", [1.0, None])
def test_open_abs_short_bar_holds_and_long_bar_peels(h):
    p = Profile.from_dict(ABS_OPEN)
    short, long = (warp.simulate(bar(n), [0, 0, 1], p, h=h, adhesion=True)["adhesion"] for n in (40, 90))
    assert not short["solver_failed"] and not long["solver_failed"]
    assert not short["may_lift"] and long["may_lift"]
    assert short["grip_n_mm"] == long["grip_n_mm"] > 0
    assert "start_height_mm" not in short
    assert 0 < long["start_height_mm"] <= 3
    assert min(abs(long["point"][0] - x) for x in (0, 90)) < 2


def test_peel_starts_low_on_a_tall_bar():
    p = Profile.from_dict(ABS_OPEN)
    adhesion = warp.simulate(bar(90, height=12), [0, 0, 1], p, h=1, adhesion=True)["adhesion"]
    assert adhesion["may_lift"] and not adhesion["solver_failed"]
    assert 1 <= adhesion["start_step"] <= 4
    assert adhesion["start_height_mm"] == adhesion["start_step"]
    assert min(abs(adhesion["point"][0] - x) for x in (0, 90)) < 2


def test_cohesive_peel_transfers_load_and_breaks_the_next_spring():
    k = np.eye(6)
    normal = [2, 5]
    k[np.ix_(normal, normal)] = [[2, -1], [-1, 2]]
    load = np.zeros(6)
    load[normal] = [0.9, 0.4]
    slope, peak, failure = 4, 0.1, 0.2
    elastic_opening = np.linalg.solve(k[np.ix_(normal, normal)] + slope * np.eye(2), load[normal])
    assert elastic_opening[1] < peak < elastic_opening[0] < failure

    lift, separation, converged = cohesive_bed(sparse.csr_matrix(k), load, np.arange(6).reshape(2, 3),
                                               np.ones(2), slope, peak, failure, 30.0, {})

    assert converged and np.all(separation >= failure)
    assert lift == pytest.approx(np.linalg.solve(k[np.ix_(normal, normal)], load[normal]))


def test_peel_finding_is_local_and_contributes_to_print_risk():
    api.custom_presets(printers={"my_glass": {"based_on": "prusa_mk4s", "bed_surface": "glass"}})
    try:
        api.load(to_binary_stl(bar(90)), "bar")
        up = [0, 0, 1]
        assert not any(f["rule"] == "bed_adhesion" for f in api.check(up, ABS_OPEN)["findings"])
        r = api.warping(up, ABS_OPEN)
        assert r["adhesion"]["assumed_surface"] and r["adhesion"]["surface"] == "textured_pei"
        assert r["adhesion"]["bed_temp_c"] == 100
        assert r["adhesion"]["point_print"] == r["lift_points_print"][0]
        assert "mm tall" in r["adhesion"]["detail"]
        check = api.check(up, ABS_OPEN)
        finding = next(f for f in check["findings"] if f["rule"] == "bed_adhesion")
        assert finding["title"] == "May come loose here"
        assert finding["metrics"]["start_height_mm"] == r["adhesion"]["start_height_mm"]
        assert finding["one_liner"] == r["adhesion"]["detail"]
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
        assert not r["solver_failed"] and not r["may_lift"]
        assert r["detached_area_mm2"] == 0
    finally:
        api.set_expert({})


@pytest.fixture
def bed_grips():
    def calculate(material, plate, temps, **overrides):
        api.custom_presets(
            printers={"test_plate": {"based_on": "prusa_mk4s", "bed_surface": plate}},
            materials={str(i): {"based_on": material, "bed_temp_c": t, **overrides} for i, t in enumerate(temps)},
        )
        return [warp.bed_grip(Profile.from_dict({"material": f"my_{i}", "printer": "my_test_plate"}))
                for i in range(len(temps))]

    yield calculate
    api.custom_presets()


@pytest.mark.parametrize("plate, anchors", [("glass", (0.55, 0.73, 6.51)), ("polyimide", (0.70, 0.51, 3.22))])
def test_pla_grip_uses_measured_shear_and_is_continuous_and_bounded(bed_grips, plate, anchors):
    hot = 0.55 * anchors[-1]
    for temp, shear in zip((50, 60, 70, 80), (*anchors, hot)):
        before, at, after = bed_grips("pla", plate, [temp - 0.001, temp, temp + 0.001])
        assert at["bed_temp_c"] == temp
        assert at["shear_source"] == ("measured shear anchor" if temp < 80 else "estimated hot tail")
        assert at["confidence"] == "low"
        assert at["shear_n_mm"] == shear
        assert at["grip_n_mm"] == pytest.approx(shear * 0.30 / (2 * 1.8))
        assert before["shear_n_mm"] == pytest.approx(shear, rel=0.001)
        assert after["shear_n_mm"] == pytest.approx(shear, rel=0.001)

    cold, mid, hot_mid, tail, beyond = bed_grips("pla", plate, [0, 65, 75, 100, 200])
    assert mid["shear_n_mm"] == pytest.approx(np.sqrt(anchors[1] * anchors[2]))
    assert hot_mid["shear_n_mm"] == pytest.approx(np.sqrt(anchors[2] * hot))
    assert cold["shear_n_mm"] == anchors[0] and cold["shear_source"] == "clamped extrapolation"
    assert tail["shear_n_mm"] == beyond["shear_n_mm"] == hot
    assert tail["shear_source"] == ("estimated hot tail" if plate == "glass" else "clamped extrapolation")
    assert beyond["shear_source"] == "clamped extrapolation"


def test_abs_grip_keeps_rising_to_120_c_and_pi_holds_better_than_glass(bed_grips):
    temps = [0, 30, 60, 100, 105, 110, 115, 120, 200]
    glass = bed_grips("abs", "glass", temps)
    pi = bed_grips("abs", "polyimide", temps)
    for curve, anchor in ((glass, 0.40), (pi, 0.51)):
        shear = [g["shear_n_mm"] for g in curve]
        assert shear[3] == anchor
        assert shear[0] == shear[1] < shear[2] < shear[3]
        assert all(np.diff(shear[3:-1]) > 0)
        assert shear[-1] == shear[-2]
        assert curve[-1]["shear_source"] == "clamped extrapolation"
        assert all(g["confidence"] == "low" for g in curve)
    assert all(g["grip_n_mm"] < p["grip_n_mm"] for g, p in zip(glass, pi))


def test_estimated_grip_changes_fastest_near_the_materials_softening_point(bed_grips):
    temps = [80, 85, 115, 120]
    standard = bed_grips("petg", "glass", temps)
    higher_tg = bed_grips("petg", "glass", temps, heat_limit_c=115)
    for curve in (standard, higher_tg):
        assert curve[0]["shear_n_mm"] == 0.73
        assert all(g["confidence"] == "low" for g in curve)
    early, late = (curve[1]["shear_n_mm"] / curve[0]["shear_n_mm"] for curve in (standard, higher_tg))
    assert early > late > 1
    assert higher_tg[3]["shear_n_mm"] / higher_tg[2]["shear_n_mm"] == pytest.approx(early)


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
    """T-362: the same bar curls substantially more in ABS than in PLA (shrinkage 0.8 vs 0.3 %); the view
    shows it true size on one scale, so the difference must be in the numbers."""
    from test_hold import box

    api.load(box(120, 20, 5), "bar")
    pla, abs_ = (api.warping([0, 0, 1], {"material": m}) for m in ("pla", "abs"))
    # A cold chamber start increases PLA's shrink relatively more, narrowing the curl ratio.
    assert abs_["max_mm"] > 1.5 * pla["max_mm"]
    assert "curl up to" in abs_["explanation"] and abs_["verdict"] != "fine"


def test_pla_on_pei_follows_the_measured_glass_curve():
    """The default plate gets the measured jump near PLA's softening point too, not the generic curve."""
    def grip(surface, temp):
        api.custom_presets(printers={"plate": {"based_on": "bambu_p1s", "bed_surface": surface}},
                           materials={"bed": {"based_on": "pla", "bed_temp_c": temp}})
        return warp.bed_grip(Profile.from_dict({"material": "my_bed", "printer": "my_plate"}))["grip_n_mm"]

    try:
        for temp in (50, 65, 70, 80):
            assert grip("textured_pei", temp) / grip("glass", temp) == pytest.approx(0.714 / 0.73)
        assert grip("textured_pei", 70) > 5 * grip("textured_pei", 60)
    finally:
        api.custom_presets()


def test_calibrated_grip_follows_the_bed_temperature_curve():
    """The bar calibration sets the level; at another bed temperature it scales like the estimate."""
    record = {"none": {"status": "calibrated", "surface": "glass", "bed_temp_c": 60, "longest_flat_mm": 80,
                       "lower_n_mm": 0.02, "upper_n_mm": 0.08, "grip_n_mm": 0.04}}
    try:
        def grip(temp):
            api.custom_presets(printers={"plate": {"based_on": "prusa_mk4s", "bed_surface": "glass"}},
                               materials={"bars": {"based_on": "pla", "bed_temp_c": temp, "bed_grip_calibration": record}})
            p = Profile.from_dict({"material": "my_bars", "printer": "my_plate"})
            return warp.bed_grip(p), warp.bed_grip(p, calibrated=False)["grip_n_mm"]

        at_60, est_60 = grip(60)
        at_70, est_70 = grip(70)
        assert at_60["grip_n_mm"] == pytest.approx(0.04) and not at_60["calibration_temp_differs"]
        assert at_70["grip_n_mm"] == pytest.approx(0.04 * est_70 / est_60) and at_70["calibration_temp_differs"]
    finally:
        api.custom_presets()
