"""Local mode (T-483): slice with OrcaSlicer without its slowdown for layer cooling, then our cooling plan."""

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


@pytest.mark.skipif(find_orca() is None, reason="needs an installed OrcaSlicer")
def test_the_local_tool_returns_optimized_gcode(tmp_path):
    """The fixture part (a block with a Ø3 mm pin): Orca without its slowdown is faster but much hotter; the
    cooling plan leaves fewer spots too hot than Orca's own slowdown, and the file it writes passed its checks."""
    api.load((FIXTURES / "part.stl").read_bytes(), "part.stl")
    out = tmp_path / "cooled.gcode"
    r = mcp_server.optimize_print_cooling([0, 0, 1], str(out))
    m = r["minutes"]
    assert r["source"] == "cooling plan" and out.read_text().startswith("; ") and "cooling plan" in out.read_text()[:4000]
    assert m["without_slowdown"] < m["orca_with_its_slowdown"] and r["changes"]["park_moves"] + r["changes"]["speed_lines"] > 0
    hot = r["spots_too_hot"]
    assert hot["with_cooling_plan"] < hot["orca_with_its_slowdown"] < hot["without_slowdown"] and "OrcaSlicer's own" in r["summary"]
