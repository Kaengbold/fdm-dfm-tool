"""Print timing (T-472): speeding up, slowing down and corners, against hand-worked cases and against
the slicers' own time estimates on the T-471 fixtures."""

import gzip
import json
import re
import zipfile
from pathlib import Path

import numpy as np
import pytest

from fdmcore import gcode, kinematics

FIXTURES = Path(__file__).parent / "gcode"


def plan(moves: list[str], *settings: str):
    """Time a few G-code lines (OrcaSlicer-style), with ``settings`` lines (``key = value``)."""
    tp = gcode.parse("\n".join(["M83", "; FEATURE: Outer wall", *moves, *(f"; {s}" for s in settings)]))
    return tp, kinematics.timing(tp)


MARLIN = ("gcode_flavor = marlin", "default_acceleration = 1000", "machine_max_jerk_x = 8", "machine_max_jerk_y = 8")


def test_a_long_line_speeds_up_and_slows_down():
    # 100 mm at 50 mm/s, 1000 mm/s² from and to a stop: 2.5 s ramps cost v/a = 0.05 s on top of L/v.
    tp, tm = plan(["G1 X0 Y0 F3000", "G1 X100 Y0 E1 F3000"], *MARLIN)
    assert tm.t1[0] - tm.t0[0] == pytest.approx(100 / 50 + 50 / 1000, rel=0.03)  # jerk lets it start a little faster
    assert tm.model == "jerk"


def test_a_short_line_never_reaches_its_speed():
    tp, tm = plan(["SET_VELOCITY_LIMIT ACCEL=1000 MINIMUM_CRUISE_RATIO=0", "G1 X0 Y0 F30000", "G1 X2 Y0 E0.1 F30000"])
    assert tm.t1[0] - tm.t0[0] == pytest.approx(2 * np.sqrt(2 / 1000), rel=1e-6)  # a triangle: up half way, down again
    _, default = plan(["SET_VELOCITY_LIMIT ACCEL=1000", "G1 X0 Y0 F30000", "G1 X2 Y0 E0.1 F30000"])
    assert default.t1[0] - default.t0[0] > tm.t1[0] - tm.t0[0]  # Klipper's default cruise ratio 0.5: a lower peak


def test_straight_on_costs_nothing_and_corners_slow_down():
    straight = plan(["G1 X0 Y0 F3000", "G1 X50 Y0 E1", "G1 X100 Y0 E1"], *MARLIN)[1]
    one = plan(["G1 X0 Y0 F3000", "G1 X100 Y0 E1"], *MARLIN)[1]
    assert straight.t1[-1] - straight.t0[0] == pytest.approx(one.t1[0] - one.t0[0], rel=1e-9)
    corner = plan(["G1 X0 Y0 F3000", "G1 X50 Y0 E1", "G1 X50 Y50 E1"], *MARLIN)[1]
    assert corner.moves["v1"][-2] == pytest.approx(8)  # a right angle: the jerk limit, 8 mm/s
    assert corner.t1[-1] - corner.t0[0] > straight.t1[-1] - straight.t0[0]


def test_klipper_square_corner_velocity():
    _, tm = plan(["SET_VELOCITY_LIMIT ACCEL=3000 SQUARE_CORNER_VELOCITY=5", "G1 X0 Y0 F6000", "G1 X40 Y0 E1", "G1 X40 Y40 E1"])
    assert tm.model == "klipper"
    assert tm.moves["v1"][-2] == pytest.approx(5)  # Klipper's corner speed at 90° is square_corner_velocity
    _, wide = plan(["SET_VELOCITY_LIMIT ACCEL=3000 SQUARE_CORNER_VELOCITY=5", "G1 X0 Y0 F6000", "G1 X40 Y0 E1", "G1 X80 Y10 E1"])
    assert wide.moves["v1"][-2] > 20  # a gentle bend barely slows


def test_retractions_and_pauses_stop_the_nozzle_and_take_time():
    base = ["G1 X0 Y0 F3000", "G1 X50 Y0 E1"]
    _, plain = plan(base + ["G1 X100 Y0 E1"], *MARLIN)
    _, paused = plan(base + ["G1 E-0.8 F1800", "G4 S2", "G1 E0.8 F1800", "G1 X100 Y0 E1"], *MARLIN)
    pause = 2 + 2 * 0.8 / 30
    assert paused.t1[-1] - plain.t1[-1] > pause  # the pause, the retraction both ways, and stopping for them
    assert paused.t0[-1] - paused.t1[0] == pytest.approx(pause, rel=1e-6)
    assert paused.moves["v1"][0] == pytest.approx(8)  # stops (down to the jerk speed)


def test_nozzle_follows_the_moves():
    tp, tm = plan(["G1 X0 Y0 F3000", "G1 X100 Y0 E1", "G1 X100 Y20 F6000", "G1 X0 Y20 E1 F3000"], *MARLIN)
    at = kinematics.nozzle_at(tp, tm, [tm.t0[0], (tm.t0[0] + tm.t1[0]) / 2, tm.t1[0], tm.t1[1]])
    assert at[0] == pytest.approx([0, 0, 0], abs=1e-6)
    assert at[1] == pytest.approx([50, 0, 0], abs=1e-6)  # symmetric profile: half way at half time
    assert at[2] == pytest.approx([100, 0, 0], abs=1e-6)
    assert at[3] == pytest.approx([0, 20, 0], abs=1e-6)
    path = kinematics.nozzle_at(tp, tm, np.linspace(tm.t0[0], tm.t1[0], 50))[:, 0]
    assert np.all(np.diff(path) > 0)
    assert len(tm.travel_t0) == 1 and tm.t1[0] <= tm.travel_t0[0] < tm.travel_t1[0] <= tm.t0[1]


def _fixture(name: str):
    if name == "bambu":
        with zipfile.ZipFile(FIXTURES / "bambu.gcode.3mf") as z:
            raw = z.read("Metadata/plate_1.gcode")
            return raw.decode(), gcode.parse(raw, json.loads(z.read("Metadata/project_settings.config")))
    raw = gzip.decompress((FIXTURES / f"{name}.gcode.gz").read_bytes())
    return raw.decode(), gcode.parse(raw)


@pytest.mark.parametrize("name, model", [("orca", "klipper"), ("prusa", "jerk"), ("bambu", "jerk")])
def test_close_to_the_slicers_own_estimate(name, model):
    """Bambu Studio and OrcaSlicer estimate the model alone ("model printing time"); PrusaSlicer the
    whole file. Lines at their feed rate alone come out 20–30 % too fast on these small parts."""
    text, tp = _fixture(name)
    tm = kinematics.timing(tp)
    assert tm.model == model
    if name == "prusa":
        ours, theirs = tm.total_s, gcode._seconds(re.search(r"estimated printing time \(normal mode\) = (.+)", text).group(1))
    else:
        ours, theirs = kinematics.print_seconds(tp, tm), gcode._seconds(re.search(r"model printing time: ([^;\n]+)", text).group(1))
    assert ours == pytest.approx(theirs, rel=0.08)
    assert np.all(tm.t1 > tm.t0) and np.all(np.diff(tm.moves["t0"]) >= 0)


def test_big_prints_are_planned_in_pieces(monkeypatch):
    """Pieces end where the nozzle stops anyway, so the times stay the same."""
    _, tp = _fixture("orca")
    whole = kinematics.timing(tp)
    monkeypatch.setattr(kinematics, "PIECE_MOVES", 400)
    pieces = kinematics.timing(tp)
    assert pieces.total_s == pytest.approx(whole.total_s, rel=0.002)
    assert np.allclose(pieces.t0, whole.t0, atol=0.5)
