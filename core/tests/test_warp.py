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
        assert r["adhesion"]["bed_temp_c"] == 100
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
    """T-362: the same bar curls several times more in ABS than in PLA (shrinkage 0.8 vs 0.3 %); the view
    shows it true size on one scale, so the difference must be in the numbers."""
    from test_hold import box

    api.load(box(120, 20, 5), "bar")
    pla, abs_ = (api.warping([0, 0, 1], {"material": m}) for m in ("pla", "abs"))
    assert abs_["max_mm"] > 2.5 * pla["max_mm"]
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
