"""The optimizer behind the new UI (T-522, plan research/redesign-2026-11/PLAN-COMBINED.md §10, §11, §14, §16)."""

from __future__ import annotations

from pathlib import Path

import pytest

from fdmcore import api, optimizer
from fdmcore.profile import Profile

SAMPLES = Path(__file__).resolve().parents[2] / "web" / "samples"


@pytest.fixture(scope="module")
def hook():
    """The hook held at its back and pulled down at the top, on a coarse grid (the tests check the logic, not accuracy)."""
    api.set_expert({"analysis.strength_voxels": 2500})
    api.load((SAMPLES / "hook.stl").read_bytes(), "hook")
    info = api.regions()["regions"]
    back = min(info, key=lambda i: i["normal"][1])["id"]
    top = max([i for i in info if i["normal"][2] > 0.99], key=lambda i: i["area_mm2"])["id"]
    yield [{"region": back, "kind": "fixed"}, {"region": top, "kind": "force", "kg": 5, "direction": "down"}]
    api.set_expert({})


def test_targets_are_floors():
    assert [optimizer.target_for(g, False) for g in ("strong", "balanced", "fast", "light", "quality")] == [5, 3, 2, 1.5, None]
    # A load that stays on creeps: at least ×3 whatever the goal, Strong keeps its higher floor.
    assert [optimizer.target_for(g, True) for g in ("strong", "balanced", "fast", "light")] == [5, 3, 3, 3]


@pytest.mark.parametrize("goal", ["balanced", "fast", "light", "strong"])
def test_goal_options_meet_the_target_and_differ(hook, goal):
    r = api.optimize(goal, hook, "fast", {"printer": "bambu_p1s"})
    assert r["target"] == optimizer.GOAL_FLOOR[goal]
    opts = r["options"]
    assert 1 <= len(opts) <= 4 and opts[0]["recommended"]
    if r["feasible"]:
        assert all(o["meets_target"] and o["safety"] >= r["target"] for o in opts)
    for a in opts:  # never padded with near copies
        assert all(optimizer._distinct(a, b) for b in opts if b is not a)
    o = opts[0]
    assert o["settings"]["wall_loops"] == str(o["walls"]) and o["total_g"] >= o["part_g"]
    assert len(o["pros"]) <= 3 and len(o["cons"]) <= 3


def test_material_lock_and_ticked_nozzles(hook):
    r = api.optimize("balanced", hook, "extended", {"printer": "bambu_p1s"}, nozzles=["0.4", "0.6"], constraints={"material": "pla"})
    assert {o["material"] for o in r["options"]} == {"pla"}
    assert {o["nozzle"] for o in r["options"]} <= {"0.4", "0.6"}  # 0.8 / 1.0 weren't ticked
    assert all(n["nozzle"] in ("0.6",) for n in r["nozzles_skipped"])


def test_your_choices_are_hard_limits(hook):
    r = api.optimize("light", hook, "fast", {"printer": "bambu_p1s"}, constraints={"walls": 3, "infill": 0.2, "up": [1, 0, 0]})
    assert all(o["walls"] == 3 and o["infill"] == 0.2 and o["up"] == [1.0, 0.0, 0.0] for o in r["options"])


def test_continuous_load_raises_the_target(hook):
    r = api.optimize("light", hook, "fast", {"printer": "bambu_p1s", "constant_load": True})
    assert r["target"] == 3.0


def test_nothing_reaches_the_target_is_honest(hook):
    heavy = [hook[0], {**hook[1], "kg": 5000}]
    r = api.optimize("strong", heavy, "fast", {"printer": "bambu_p1s"}, constraints={"material": "pla"})
    assert not r["feasible"]
    best = r["options"][0]
    assert best["below_target"] and best["safety"] < 5
    ids = [w["id"] for w in r["ways_out"]]
    assert ids[:2] == ["allow_materials", "allow_design"] and "use_anyway" in ids
    assert "None of the options we checked reached ×5" in r["explanation"]


def test_quality_and_no_loads_never_solve(hook, monkeypatch):
    from fdmcore import strength

    def no_solve(*a, **k):
        raise AssertionError("strength solve in Quality / without loads")

    monkeypatch.setattr(strength, "_solve", no_solve)
    q = api.optimize("quality", hook, "fast", {"printer": "bambu_p1s"})
    assert q["target"] is None and q["options"] and all(o["safety"] is None for o in q["options"])
    assert q["options"][0]["walls"] == 2 and q["options"][0]["infill"] == pytest.approx(0.10)  # §14: start light
    n = api.optimize("balanced", None, "fast", {"printer": "bambu_p1s"})
    assert any(s["what"] == "strength" for s in n["skipped"])


def test_environment_filters_materials(hook):
    r = api.optimize("balanced", None, "fast", {"printer": "bambu_p1s"}, environment={"temp_c": 90, "touches": ["solvent"]})
    skipped = {m["material"] for m in r["materials_skipped"]}
    assert {"pla", "petg"} <= skipped  # too hot for them
    assert "abs" in skipped  # poor with cleaners (and acetone)
    assert all(o["material"] not in skipped for o in r["options"])


def test_design_only_keeps_the_recipe(hook):
    r = api.optimize("balanced", hook, "fast", {"printer": "bambu_p1s"}, change="design")
    plan = optimizer._plan_shells(Profile(), "balanced")
    assert all(o["walls"] == plan.walls and o["infill"] == pytest.approx(plan.infill) for o in r["options"])
    assert any(s["what"] == "design additions" for s in r["skipped"])


def test_bad_arguments():
    with pytest.raises(ValueError, match="goal"):
        api.optimize("prettiest")
    with pytest.raises(ValueError, match="depth"):
        api.optimize("balanced", None, "forever")


@pytest.mark.parametrize("goal", ["balanced", "quality"])
def test_corpus_bracket(goal):
    """A corpus part: held on its biggest back face, loaded on its biggest top face."""
    corpus = Path(__file__).resolve().parent / "corpus"
    api.set_expert({"analysis.strength_voxels": 2500})
    try:
        api.load((corpus / "shelf_bracket.stl").read_bytes(), "shelf_bracket")
        info = [i for i in api.regions()["regions"] if i["kind"] == "flat"]
        back = max(info, key=lambda i: i["area_mm2"] * max(-i["normal"][0], -i["normal"][1], 0))["id"]
        top = max(info, key=lambda i: i["area_mm2"] * max(i["normal"][2], 0))["id"]
        loads = [{"region": back, "kind": "fixed"}, {"region": top, "kind": "force", "kg": 3, "direction": "down"}]
        r = api.optimize(goal, loads, "fast", {"printer": "bambu_p1s"})
        assert r["options"]
        if goal == "quality":
            assert r["target"] is None
        else:
            assert r["target"] == 3.0 and (not r["feasible"] or r["options"][0]["safety"] >= 3.0)
    finally:
        api.set_expert({})
