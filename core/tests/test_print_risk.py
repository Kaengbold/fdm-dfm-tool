"""Print failure risk: the signs added up, and a brim or a raft from them (T-391)."""

import numpy as np
from shapely.geometry import Point

from fdmcore import api
from fdmcore.additions import extrude
from fdmcore.export3mf import prusa_settings
from fdmcore.mesh import Mesh, to_binary_stl
from fdmcore.profile import Profile
from fdmcore.recommend import print_risk, recommend_settings

TINY = {"rule": "small_bed_contact", "severity": "warn", "detail": "Only 13 mm² touches the bed (largest layer 13 mm²)"}
TIPS = {"rule": "tipping_risk", "severity": "warn", "detail": "40 mm tall on a base only 4 mm wide"}
LIFTS = {"rule": "warping", "severity": "warn", "detail": "90 mm long base in PLA — corners likely lift"}


def test_signs_add_up_to_the_risk_and_what_holds_the_part():
    pla = Profile.from_dict({})
    r = print_risk(pla, [], 20)
    assert (r["level"], r["hold"], r["reasons"]) == ("low", "no_brim", [])
    assert (print_risk(pla, [LIFTS], 20)["level"], print_risk(pla, [LIFTS], 20)["hold"]) == ("medium", "brim_ears")
    assert print_risk(pla, [{**LIFTS, "severity": "problem"}], 20)["level"] == "high"
    assert print_risk(pla, [TIPS], 40)["hold"] == "outer_only"
    # A tiny base on a part that also tips: a brim isn't enough, a raft holds it.
    r = print_risk(pla, [TINY, TIPS], 40)
    assert (r["level"], r["hold"]) == ("high", "raft") and "4 mm wide" in r["reasons"][1]


def test_tall_abs_on_an_open_printer_and_the_simulation_count():
    open_abs = Profile.from_dict({"printer": "bambu_a1", "material": "abs"})
    assert print_risk(open_abs, [], 80)["level"] == "medium"  # splits between layers as it cools
    assert print_risk(open_abs, [], 30)["level"] == "low"
    r = print_risk(open_abs, [], 30, warp_verdict="likely to lift")
    assert (r["level"], r["hold"]) == ("high", "raft") and "warping simulation" in r["reasons"][0]
    closed_abs = Profile.from_dict({"printer": "bambu_p1s", "material": "abs"})
    assert print_risk(closed_abs, [], 30, warp_verdict="likely to lift")["hold"] == "brim_ears"


def test_raft_settings_replace_the_brim():
    s = {x["key"]: x for x in recommend_settings(Profile.from_dict({}), [TINY, TIPS])}
    assert s["raft_layers"]["value"] == "3" and s["brim_type"]["value"] == "no_brim"
    assert s["elefant_foot_compensation"]["value"] == "0"
    assert "4 mm wide" in s["raft_layers"]["reason"]
    assert prusa_settings({"raft_layers": "3"}) == {"raft_layers": "3"}


def test_a_thin_tall_pin_gets_the_finding_and_a_raft():
    pin = extrude(Point(0, 0).buffer(2, 32), 0, 40)
    api.load(to_binary_stl(Mesh(pin)), "pin")
    r = api.check([0, 0, 1], {})
    assert r["print_risk"]["level"] == "high" and r["print_risk"]["hold"] == "raft"
    found = [f for f in r["findings"] if f["rule"] == "print_may_fail"]
    assert len(found) == 1 and found[0]["impact"] == "fails" and found[0]["severity"] == "warn"
    assert {s["key"]: s["value"] for s in r["settings"]}["raft_layers"] == "3"
    lying = api.check([1, 0, 0], {})
    assert lying["print_risk"]["level"] == "low" and not [f for f in lying["findings"] if f["rule"] == "print_may_fail"]


def test_the_last_warping_simulation_counts_only_for_its_own_setup():
    api.load(to_binary_stl(Mesh(extrude(Point(0, 0).buffer(10, 32), 0, 5))), "disc")
    s = api._require()
    p = Profile.from_dict({"printer": "bambu_a1", "material": "abs"})
    s.warp = {"up": np.array([0.0, 0, 1]), "verdict": "likely to lift", "setup": ("abs", "none")}
    assert api._warp_verdict(s, [0, 0, 1], p) == "likely to lift"
    assert api._warp_verdict(s, [1, 0, 0], p) is None  # another orientation
    assert api._warp_verdict(s, [0, 0, 1], Profile.from_dict({"printer": "bambu_a1", "material": "pla"})) is None
    assert api.check([0, 0, 1], {"printer": "bambu_a1", "material": "abs"})["print_risk"]["hold"] == "raft"
