import pytest

from fdmcore import api
from fdmcore.profile import Profile
from fdmcore.service import heat_factor

from test_strength import SAMPLES, hook_loads


def test_heat_factor_follows_the_measured_pla_curve():
    at = lambda t: heat_factor(Profile.from_dict({"material": "pla", "use_temp_c": t}))
    assert at(None) == at(23) == 1.0
    assert at(40) == pytest.approx(0.82, abs=0.02)  # 1143 / 1400 MPa at 40 °C
    assert at(60) == pytest.approx(0.25, abs=0.01)  # at the glass transition
    assert heat_factor(Profile.from_dict({"material": "petg", "use_temp_c": 60})) > at(60)


def test_hot_part_is_weaker_and_explained():
    loads = hook_loads()
    room = api.strength([1, 0, 0], loads, {"material": "pla"})
    car = api.strength([1, 0, 0], loads, {"material": "pla", "use_temp_c": 65})
    assert car["safety_factor"] < 0.3 * room["safety_factor"]
    assert "goes soft" in car["explanation"]


def test_constant_load_needs_more_margin():
    fixed, force = hook_loads()
    loads = [fixed, {**force, "kg": 6.5}]
    r = api.strength([1, 0, 0], loads, {"material": "pla"})
    sf = r["safety_factor"]
    assert 2 <= sf < 3, sf  # the hook in PLA lies between the two margins
    stays = api.strength([1, 0, 0], loads, {"material": "pla", "constant_load": True})
    assert r["verdict"] == "fine" and stays["verdict"] == "borderline"
    assert "creep" in stays["explanation"]


def test_heat_finding():
    api.load((SAMPLES / "hook.stl").read_bytes(), "hook")
    rules = lambda prof: {f["rule"]: f["severity"] for f in api.check([1, 0, 0], prof, "both")["findings"]}
    assert "too_hot_in_use" not in rules({"material": "pla", "use_temp_c": 30})
    assert rules({"material": "pla", "use_temp_c": 50})["too_hot_in_use"] == "warn"
    assert rules({"material": "pla", "use_temp_c": 70})["too_hot_in_use"] == "problem"
    assert "too_hot_in_use" not in rules({"material": "asa", "use_temp_c": 70})
