"""Screws, nuts and heat-set inserts in a hole the user picked (T-320)."""

from pathlib import Path

import numpy as np
import pytest
from shapely.geometry import Point

from fdmcore import api
from fdmcore.additions import extrude
from fdmcore.mesh import Mesh, to_binary_stl

from test_features import _clean

SAMPLES = Path(__file__).resolve().parents[2] / "web" / "samples"
UP = [0, 0, 1]


def _bracket_hole() -> int:
    """The L-bracket's Ø6 mm hole standing upright, 5 mm long."""
    api.load((SAMPLES / "l_bracket.stl").read_bytes(), "l_bracket")
    hole = next(f for f in api.design(UP)["features"] if f["type"] == "hole" and abs(f["axis"][2]) > 0.9)
    assert hole["diameter"] == pytest.approx(6.0, abs=0.05)
    return hole["region"]


def _applied(prefix: str = "fastener"):
    from fdmcore.booleans import apply_edits

    edits = [e for e in api.design(UP)["edits"] if e["id"].startswith(prefix)]
    before = api._require().mesh
    return edits, before, apply_edits(before, edits)


def test_insert_hole_size_depth_and_a_clean_result():
    pytest.importorskip("manifold3d")
    hole = _bracket_hole()
    r = api.set_fastener(hole, UP, "M3", "insert")
    assert r["target_mm"] == 4.0 and r["need_depth_mm"] == pytest.approx(6.2)
    assert "only 5.0 mm long" in r["explanation"]  # the M3 insert is 5.7 mm long
    edits, _, after = _applied()
    assert [e["op"] for e in edits] == ["add"]  # Ø6 → Ø4: plastic added
    assert _clean(after.triangles) == (0, 0)
    api.load(to_binary_stl(after), "edited")
    holes = [f for f in api.design(UP)["features"] if f["type"] == "hole" and abs(f["axis"][2]) > 0.9]
    assert min(abs(f["diameter"] - 4.0) for f in holes) < 0.1


@pytest.mark.parametrize("use", ["nut_top", "nut_side"])
def test_nut_pocket_and_side_slot_cut_plastic_away(use):
    pytest.importorskip("manifold3d")
    from fdmcore.booleans import apply_edits

    hole = _bracket_hole()
    r = api.set_fastener(hole, UP, "M3", use)
    p = r["pocket"]
    assert p["kind"] == ("top" if use == "nut_top" else "side")
    edits, before, after = _applied()
    resized = apply_edits(before, [e for e in edits if not e["id"].startswith("fastener-nut")])
    # The hex (minus the Ø3.5 bolt hole) at least: a side slot adds its way out.
    nut = (np.sqrt(3) / 2 * p["across_flats_mm"] ** 2 - np.pi * 1.75**2) * p["depth_mm"]
    assert abs(resized.volume()) - abs(after.volume()) > 0.9 * nut
    assert _clean(after.triangles) == (0, 0)


def test_thin_wall_gets_a_boss_and_more_walls():
    tube = extrude(Point(0, 0).buffer(2.5, 64).difference(Point(0, 0).buffer(1.5, 64)), 0, 10)
    api.load(to_binary_stl(Mesh(tube)), "tube")
    hole = next(f for f in api.design(UP)["features"] if f["type"] == "hole")
    r = api.set_fastener(hole["region"], UP, "M3", "screw", {"nozzle": "0.4"})
    assert r["target_mm"] == 2.5 and r["wall_mm"] == pytest.approx(1.0, abs=0.1)
    assert r["walls"] >= 6 and "walls" in r["explanation"]
    assert any(e["id"].startswith("fastener-boss") for e in api.design(UP)["edits"])


def test_bolt_hole_and_forgetting():
    hole = _bracket_hole()
    r = api.set_fastener(hole, UP, "M4", "bolt")
    assert r["target_mm"] == 4.5 and "Ø9 mm" in r["explanation"]  # ISO 273 medium clearance, ISO 7089 washer
    api.set_fastener(hole, UP, None)
    assert not [e for e in api.design(UP)["edits"] if e["id"].startswith("fastener")]
    with pytest.raises(ValueError, match="size must be"):
        api.set_fastener(hole, UP, "M7", "bolt")
