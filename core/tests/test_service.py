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
    loads = [fixed, {**force, "kg": 7.5}]
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


def test_box_heat():
    """An 80 × 60 × 40 mm box: 208 cm² outside; a few watts in a closed PLA box get it warm, a fan
    or a material that takes more heat fixes it."""
    from test_hold import box

    api.load(box(80, 60, 40), "box")
    closed = api.box_heat(2, 25, "closed", {"material": "pla"})
    assert closed["area_cm2"] == pytest.approx(208)
    assert 15 < closed["rise_c"] < 30 and closed["verdict"] == "warm"
    assert api.box_heat(2, 25, "fan", {"material": "pla"})["verdict"] == "fine"
    assert api.box_heat(5, 25, "closed", {"material": "pla"})["verdict"] == "too hot"
    assert api.box_heat(5, 25, "closed", {"material": "asa"})["verdict"] == "fine"
    with pytest.raises(ValueError):
        api.box_heat(1, 25, "vents")


def test_heat_curve_for_the_chart():
    """Full strength from winter to room, falling towards the heat limit; the marked temperature is
    the one asked for (or the profile's), and cold is called brittle, not weaker."""
    r = api.heat_curve({"material": "pla", "use_temp_c": 55})
    temps, share = r["temps_c"], r["share_pct"]
    assert temps[0] == -20 and temps[-1] >= r["soft_c"] + 20 and temps == sorted(temps)
    assert share[temps.index(r["room_c"])] == 100 and share[-1] < 10
    assert all(a >= b for a, b in zip(share, share[1:]))  # never stronger when warmer
    assert r["use_temp_c"] == 55 and 30 < r["share_at_pct"] < 70 and "55 °C" in r["explanation"]
    cold = api.heat_curve({"material": "pla"}, use_temp_c=-20)
    assert cold["share_at_pct"] == 100 and "brittle" in cold["explanation"]
    assert api.heat_curve({"material": "pla"})["use_temp_c"] == r["room_c"]
