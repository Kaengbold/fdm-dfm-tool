"""The cooling plan (T-480): cool spots that stay too hot, undo slowdowns that weren't needed."""

import gzip
from pathlib import Path

import numpy as np
import pytest

from fdmcore import api, gcode, kinematics, thermal_opt
from fdmcore.gcode import BRIDGE, OVERHANG
from fdmcore.profile import Profile
from test_heat_window import slab

FIXTURES = Path(__file__).parent / "gcode"
P = Profile.from_dict({"material": "pla", "printer": "bambu_p1s"})


@pytest.fixture(scope="module")
def needless():
    """A load-bearing 6 mm wall the slicer slowed from 200 to 20 mm/s for layer cooling, at full fan."""
    tp = gcode.parse(slab(fan=255, speed=20, set_speed=200))
    return tp, thermal_opt.optimize(tp, P, lambda h: np.ones(len(h.layer)))


@pytest.fixture(scope="module")
def pin():
    """The fixture part sliced in OrcaSlicer: its Ø3 mm pin stays too hot even with Orca's slowdown."""
    tp = gcode.parse(gzip.decompress((FIXTURES / "orca.gcode.gz").read_bytes()))
    return tp, thermal_opt.optimize(tp, P)


def test_undoing_a_needless_slowdown_is_faster_and_bonds_better(needless):
    """Faster than the slicer's cooling slowdown with an equal or better window: no spot gets too hot, the
    bond improves, and the speed-up stays within its cap. A slicer project can't undo the slowdown."""
    _, r = needless
    before, after = r["before"], r["gcode"]
    assert after["print_s"] < 0.8 * before["print_s"]
    assert after["print_s"] >= (1 - thermal_opt.settings()["max_speedup"]) * before["print_s"] * 0.97  # line times are estimated
    assert after["too_hot"] == 0 and after["weighted_cold"] <= before["weighted_cold"] and after["bond_median"] > before["bond_median"]
    assert any(row["faster"] > 1 for row in after["plan"])
    assert r["slicer"]["print_s"] == pytest.approx(before["print_s"]) and r["slicer"]["cooling"] == {"ranges": []}


def test_a_pin_that_stays_too_hot_is_cooled(pin):
    """Fan and speed can't cool the pin (Orca already slowed it, the fan is full): short park moves can. The
    slicer plan does the same with a cooling tower; neither makes anything too cold."""
    _, r = pin
    assert r["before"]["too_hot"] > 0
    for plan in ("gcode", "slicer"):
        assert r[plan]["too_hot"] == 0 and r[plan]["too_cold"] == 0
    assert any(row["park_s"] > 0 and row["z_mm"][0] > 6 for row in r["gcode"]["plan"])  # above the 6 mm block: the pin
    tower = r["slicer"]["cooling"]["tower"]
    assert 8 <= tower["diameter"] <= 25 and tower["height"] > 6


def test_auxiliary_boost_requires_an_equipped_printer(pin):
    tp, r = pin
    assert r["aux_fan_estimated"]
    assert any(row["aux_fan"] == 1 for row in r["gcode"]["plan"])
    assert r["plans"]["slicer"].aux is None
    a1 = Profile.from_dict({"printer": "bambu_a1"})
    unsupported = thermal_opt.optimize(tp, a1)
    assert not unsupported["aux_fan_estimated"]
    assert unsupported["plans"]["gcode"].aux is None


def test_the_plan_leaves_protected_lines_alone(pin):
    """Overhang and bridge slowdowns, layers 0–1 and anything not part of the part keep their speed; nothing
    goes faster than its speed setting; fan commands stay in order; park moves add travel and pauses."""
    tp, _ = pin
    layers = int(tp.layer.max()) + 1
    plan = thermal_opt.Plan.none(layers)
    plan.slow[:] = 0.5
    plan.fast[:] = 10.0
    plan.fan[:] = 1.0
    plan.park[10] = 6.0
    out = thermal_opt.apply(tp, plan)
    fixed = np.isin(tp.cause, (OVERHANG, BRIDGE)) | (tp.layer < 2) | ~np.isin(tp.kind, gcode.PART_KINDS)
    assert np.array_equal(out.speed[fixed], tp.speed[fixed])
    assert (out.speed <= np.nan_to_num(tp.set_speed, nan=np.inf) * 1.0001).all() | (out.speed == tp.speed).all()
    assert (np.diff(out.fans["line"]) >= 0).all() and set(np.unique(out.fans["value"])) <= {0.0, 1.0} | set(np.unique(tp.fans["value"]))
    assert len(out.travel["t0"]) == len(tp.travel["t0"]) + 2 and out.dwells["s"].sum() == pytest.approx(tp.dwells["s"].sum() + 6.0)
    plan.park[10] = 0.0
    assert kinematics.timing(out).total_s - kinematics.timing(thermal_opt.apply(tp, plan)).total_s == pytest.approx(6.0, abs=1.0)


def test_optimize_cooling_in_plain_words():
    api.load((FIXTURES / "part.stl").read_bytes(), "fixture")
    api.load_gcode((FIXTURES / "bambu.gcode.3mf").read_bytes(), [0, 0, 1])
    r = api.optimize_cooling([0, 0, 1], {"material": "pla", "printer": "bambu_p1s"})
    assert r["confidence"] == "rough" and r["guessed"]
    assert [r[plan]["too_hot"] for plan in ("before", "gcode", "slicer")] == [40, 40, 10]
    assert [r[plan]["too_cold"] for plan in ("before", "gcode", "slicer")] == [0, 0, 0]
    assert r["summary"][0].startswith("As sliced") and any("cooling plan in the G-code" in line for line in r["summary"])
    assert "Rough" in r["summary"][-1]
    cooling = r["slicer"]["cooling"]
    assert set(cooling) <= {"ranges", "tower"} and all({"z0", "z1", "speeds"} <= set(x) for x in cooling["ranges"])
