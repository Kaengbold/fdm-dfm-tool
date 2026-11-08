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
