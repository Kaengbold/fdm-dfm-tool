"""The cooling plan written into the user's own G-code (T-481): only speeds, part fan and park moves change."""

import gzip
import re
from pathlib import Path

import numpy as np
import pytest

from fdmcore import api, gcode, gcode_rewrite, thermal_opt
from test_heat_window import slab

FIXTURES = Path(__file__).parent / "gcode"


def absolute_e(text: str) -> str:
    """The same G-code with absolute extrusion (M82): each E becomes the running total."""
    total, out = 0.0, []
    for line in text.splitlines():
        if line == "M83":
            out.append("M82")
            continue
        m = re.search(r" E([-\d.]+)", line)
        if line.startswith("G1") and m:
            total += float(m.group(1))
            line = line[:m.start()] + f" E{total:.5f}" + line[m.end():]
        out.append(line)
    return "\n".join(out)


@pytest.mark.parametrize("absolute", [False, True])
def test_speeds_fan_and_parks_change_nothing_else(absolute):
    """Slower layers, more fan and a park move: read back, the paths and the plastic per layer are the
    same, the speeds and fan are the plan's, travel keeps its speed, and the park adds its pause. Works with
    relative and absolute extrusion."""
    text = slab(fan=128, speed=40, set_speed=40)
    text = absolute_e(text) if absolute else text
    tp = gcode.parse(text)
    plan = thermal_opt.Plan.none(int(tp.layer.max()) + 1)
    plan.slow[8:14] = 0.5
    plan.fan[10:14] = 1.0
    plan.park[16] = 6.0
    new = gcode_rewrite.rewrite(text, tp, plan)
    assert gcode_rewrite.check(tp, new.text, new.toolpath) == []
    back = gcode.parse(new.text)
    assert np.allclose(back.p0, tp.p0) and np.allclose(back.volume, tp.volume, rtol=1e-4)
    assert np.allclose(back.speed, new.toolpath.speed, rtol=1e-3) and (back.speed[tp.layer == 10] < tp.speed[tp.layer == 10]).all()
    assert np.allclose(back.part_fan, new.toolpath.part_fan, atol=1 / 255)
    original_travel = np.median(tp.travel["speed"])
    assert np.median(back.travel["speed"]) == pytest.approx(original_travel)
    assert back.dwells["s"].sum() == pytest.approx(tp.dwells["s"].sum() + 6.0)
    assert new.parks == 1 and new.minutes[1] > new.minutes[0]
    assert new.text.startswith("; FDM DFM Tool cooling plan")


def test_progress_lines_follow_the_new_timing():
    data = gzip.decompress((FIXTURES / "orca.gcode.gz").read_bytes())
    tp = gcode.parse(data)
    plan = thermal_opt.Plan.none(int(tp.layer.max()) + 1)
    plan.slow[10:30] = 0.5
    new = gcode_rewrite.rewrite(data, tp, plan)
    old = [line for line in data.decode().splitlines() if line.startswith("M73 P")]
    now = [line for line in new.text.splitlines() if line.startswith("M73 P")]
    assert len(old) == len(now) and old[0] == now[0]  # the start G-code is left as it is
    assert any(a != b for a, b in zip(old, now)) and now[-1].startswith("M73 P100 R0")
    assert "HEADER_BLOCK_END" in new.text.split("; FDM DFM Tool cooling plan")[0]  # our note goes after the slicer's header


def test_a_broken_rewrite_is_refused():
    text = slab(fan=128, speed=40, set_speed=40)
    tp = gcode.parse(text)
    plan = thermal_opt.Plan.none(int(tp.layer.max()) + 1)
    plan.slow[8:14] = 0.5
    new = gcode_rewrite.rewrite(text, tp, plan)
    more_plastic = re.sub(r" E(\d+\.\d+)", lambda m: f" E{float(m.group(1)) * 1.1:.5f}", new.text, count=50)
    assert "The plastic per layer changed" in gcode_rewrite.check(tp, more_plastic, new.toolpath)
    moved = new.text.replace("X60.000", "X61.000", 1)
    assert gcode_rewrite.check(tp, moved, new.toolpath) == ["The printed paths changed"]
    too_fast = re.sub(r"F2400\b", "F240000", new.text)
    assert any("speed" in p for p in gcode_rewrite.check(tp, too_fast, new.toolpath))


def test_optimize_gcode_offers_a_checked_file():
    data = gzip.decompress((FIXTURES / "orca.gcode.gz").read_bytes())
    api.load((FIXTURES / "part.stl").read_bytes(), "fixture")
    api.load_gcode(data, [0, 0, 1])
    r = api.optimize_gcode([0, 0, 1], data)
    assert r["ok"] and r["changes"]["park_moves"] > 0 and r["minutes"][1] > r["minutes"][0]
    text = r["data"].decode()
    assert "Spots too hot 40 -> 0" in text and gcode.parse(text).dwells["s"].sum() > 0
    with pytest.raises(ValueError, match="Plain .gcode"):
        api.optimize_gcode([0, 0, 1], (FIXTURES / "bambu.gcode.3mf").read_bytes())
    with pytest.raises(ValueError, match="isn't the G-code"):
        api.optimize_gcode([0, 0, 1], slab().encode())
