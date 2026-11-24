"""Local mode (T-483): slice with OrcaSlicer without its slowdown for layer cooling, then our cooling plan."""

import re

import pytest

from fdmcore import api, mcp_server
from fdmcore.slicer_cli import find_orca, without_cooling_slowdown
from test_heat_window import FIXTURES


def test_only_the_cooling_slowdown_is_switched_off():
    chosen = {"machine": {"name": "m", "data": {"a": "1"}}, "process": {"name": "p", "data": {}},
              "filament": {"name": "f", "data": {"slow_down_for_layer_cooling": ["1", "1"], "fan_max_speed": ["100"]}}}
    off = without_cooling_slowdown(chosen)
    assert off["filament"]["data"] == {"slow_down_for_layer_cooling": ["0", "0"], "fan_max_speed": ["100"]}
    assert chosen["filament"]["data"]["slow_down_for_layer_cooling"] == ["1", "1"]  # the original stays as it was
    assert off["machine"] is chosen["machine"] and off["process"] is chosen["process"]


@pytest.fixture
def slice_with_chamber(monkeypatch):
    api.load((FIXTURES / "part.stl").read_bytes(), "part.stl")
    original_slice = mcp_server._slice

    def set_startup(command):
        slices = {}

        def slice_part(*args, cooling_slowdown=True, **kwargs):
            result = original_slice(*args, cooling_slowdown=cooling_slowdown, **kwargs)
            sliced = result["orcaslicer"]
            sliced["gcode"] = f"{command} S35\n" + sliced["gcode"]
            slices[cooling_slowdown] = sliced["gcode"]
            return result

        monkeypatch.setattr(mcp_server, "_slice", slice_part)
        return slices

    return set_startup


@pytest.mark.skipif(find_orca() is None, reason="needs an installed OrcaSlicer")
def test_the_local_tool_returns_checked_gcode(tmp_path, slice_with_chamber):
    """With the chamber preheated, the checked plan beats Orca's own slowdown on the hot pin."""
    slice_with_chamber("M191")
    out = tmp_path / "cooled.gcode"
    r = mcp_server.optimize_print_cooling([0, 0, 1], str(out))
    m = r["minutes"]
    assert r["source"] == "cooling plan", r
    assert "; FDM DFM Tool cooling plan" in out.read_text(encoding="utf-8")[:4000]
    assert m["without_slowdown"] < m["orca_with_its_slowdown"]
    assert r["changes"]["park_moves"] + r["changes"]["speed_lines"] > 0
    hot = r["spots_too_hot"]
    assert hot["with_cooling_plan"] < hot["orca_with_its_slowdown"] < hot["without_slowdown"] and "OrcaSlicer's own" in r["summary"]


@pytest.mark.skipif(find_orca() is None, reason="needs an installed OrcaSlicer")
def test_the_local_tool_keeps_orcas_slice_when_the_cold_start_fails_the_check(tmp_path, slice_with_chamber):
    slices = slice_with_chamber("M141")
    out = tmp_path / "fallback.gcode"
    r = mcp_server.optimize_print_cooling([0, 0, 1], str(out))
    assert r["source"] == "OrcaSlicer's own", r
    assert re.search(r"\b[1-9]\d* newly too cold to bond", r["why"]), r
    assert out.read_text(encoding="utf-8") == slices[True]
