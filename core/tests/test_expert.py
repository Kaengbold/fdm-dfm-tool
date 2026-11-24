"""Expert settings window: every number listed with its note, changes reach the checks, reset works."""

import pytest

from fdmcore import api

from test_hold import box, region_facing
from test_strength import SAMPLES


def test_every_threshold_is_listed_with_its_source():
    sections = {s["name"]: s for s in api.expert_settings()["sections"]}
    assert "analysis" in sections and "features" in sections and "warping" in sections
    vanish = next(v for v in sections["features"]["values"] if v["path"] == "thresholds.features.vanish_below_nozzle")
    assert vanish["value"] == vanish["default"] == 0.75 and "min_arachne" in vanish["note"]
    assert sum(len(s["values"]) for s in sections.values()) > 100


def test_saved_expert_settings_can_contain_the_retired_peel_gate():
    old_path = "thresholds.bed_adhesion.min_curl_layers"
    active_path = "thresholds.features.vanish_below_nozzle"
    try:
        result = api.set_expert({old_path: 3.0, active_path: 0.6})
        values = {v["path"]: v["value"] for section in result["sections"] for v in section["values"]}
        assert old_path not in values
        assert values[active_path] == 0.6
    finally:
        api.set_expert({})


def test_changes_reach_the_checks_and_reset():
    api.load((SAMPLES / "test_block.stl").read_bytes(), "test_block")
    thin = lambda: sum(f["rule"] == "too_thin_to_print" for f in api.check([0, 0, 1])["findings"])  # noqa: E731
    before = thin()
    try:
        api.set_expert({"thresholds.features.vanish_below_nozzle": 0.01})  # nothing counts as too thin
        assert thin() < before
        api.load(box(120, 12, 12), "bar")
        loads = [{"region": region_facing([-1, 0, 0]), "kind": "fixed"}, {"region": region_facing([1, 0, 0]), "kind": "force", "kg": 1}]
        coarse = api.strength([0, 0, 1], loads)["mesh"]["voxels"]
        api.set_expert({"analysis.strength_voxels": 3000})
        assert api.strength([0, 0, 1], loads)["mesh"]["voxels"] < coarse
        with pytest.raises(KeyError, match="Unknown setting"):
            api.set_expert({"thresholds.features.nope": 1})
    finally:
        api.set_expert({})
    api.load((SAMPLES / "test_block.stl").read_bytes(), "test_block")
    assert thin() == before
