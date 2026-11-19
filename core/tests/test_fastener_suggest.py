"""Automatic fastener sizing suggestions for standard-sized round holes (T-437)."""

import json
from pathlib import Path

import pytest
from shapely.geometry import Point, box

from fdmcore import api
from fdmcore.additions import extrude
from fdmcore.mesh import Mesh, to_binary_stl

ROOT = Path(__file__).resolve().parents[2]
SAMPLES = ROOT / "web" / "samples"
CORPUS = Path(__file__).parent / "corpus"
UP = [0, 0, 1]


def _plate_with_holes(diameters: list[float], thickness: float = 10.0) -> bytes:
    """Flat plate with one upright through-hole per diameter, spaced well apart."""
    shape = box(-15, -15, 25 * len(diameters), 15)
    for i, d in enumerate(diameters):
        shape = shape.difference(Point(i * 25, 0).buffer(d / 2, 48))
    return to_binary_stl(Mesh(extrude(shape, 0.0, thickness)))


def test_enclosure_one_m3_suggestion_covering_all_four_holes():
    api.load((SAMPLES / "enclosure.stl").read_bytes(), "enclosure")
    res = api.dispatch("fastener_suggestions", json.dumps({"up": UP}))
    json.dumps(res)  # must be JSON-friendly
    assert len(res["suggestions"]) == 1
    s = res["suggestions"][0]
    assert s["size"] == "M3" and s["count"] == 4 and len(s["regions"]) == 4
    assert s["diameter_mm"] == pytest.approx(2.8, abs=0.05)
    assert "All four" in s["prompt"] and "M3" in s["prompt"] and "Ø2.8" in s["prompt"]
    assert s["choices"][0]["use"] == "screw" and s["choices"][0]["target_mm"] == 2.5
    assert {"screw", "bolt", "insert", "nut_top", "nut_side"} <= {c["use"] for c in s["choices"]}
    assert s["applied"] is None

    # Applying the suggestion sizes all four holes via the existing set_fastener path.
    for r in s["regions"]:
        plan = api.set_fastener(r, UP, s["size"], "screw")
        assert plan["target_mm"] == 2.5
    after = api.fastener_suggestions(UP)["suggestions"]
    assert len(after) == 1 and after[0]["applied"] == {"size": "M3", "use": "screw"}
    resize_edits = [e for e in api.design(UP)["edits"] if e["id"] in {f"fastener-{r}" for r in s["regions"]}]
    assert len(resize_edits) == 4


def test_detail_plate_matches_standard_and_skips_tiny_holes():
    api.load((SAMPLES / "detail_plate.stl").read_bytes(), "detail_plate")
    sugs = api.fastener_suggestions(UP)["suggestions"]
    by_size = {s["size"]: s for s in sugs}
    assert set(by_size) == {"M3", "M5"}  # Ø1.0 mm hole is ignored
    assert by_size["M3"]["choices"][0] == {"use": "bolt", "label": "Through bolt", "target_mm": 3.4}
    assert by_size["M5"]["choices"][0] == {"use": "bolt", "label": "Through bolt", "target_mm": 5.5}


def test_pilot_clearance_insert_and_out_of_range_holes():
    api.load(_plate_with_holes([2.5, 3.2, 8.0, 12.0]), "holes")
    sugs = api.fastener_suggestions(UP)["suggestions"]
    assert len(sugs) == 3  # Ø12.0 mm is not near any fastener size
    by_dia = {round(s["diameter_mm"], 1): s for s in sugs}
    assert by_dia[2.5]["size"] == "M3" and by_dia[2.5]["choices"][0]["use"] == "screw"
    assert by_dia[3.2]["size"] == "M3" and by_dia[3.2]["choices"][0]["use"] == "bolt"
    assert by_dia[8.0]["size"] == "M6" and [c["use"] for c in by_dia[8.0]["choices"]] == ["insert"]


def test_sideways_holes_get_sideways_clearance_extra():
    api.load(_plate_with_holes([3.0, 3.0]), "two_m3")
    # Print standing on edge: both Ø3.0 holes now lie sideways (axis Z perpendicular to up X).
    sugs = api.fastener_suggestions([1, 0, 0])["suggestions"]
    assert len(sugs) == 1 and sugs[0]["count"] == 2 and "Both" in sugs[0]["prompt"]
    bolt = next(c for c in sugs[0]["choices"] if c["use"] == "bolt")
    assert bolt["target_mm"] == 3.5  # 3.4 + 0.1 sideways_extra_mm


@pytest.mark.parametrize("path", [SAMPLES / "hole_block.stl", SAMPLES / "cable_clip.stl", CORPUS / "knob.stl"])
def test_large_or_partial_holes_produce_no_suggestions(path):
    api.load(path.read_bytes(), path.stem)
    assert api.fastener_suggestions(UP)["suggestions"] == []


def test_rejected_hole_dropped_from_group():
    api.load((SAMPLES / "enclosure.stl").read_bytes(), "enclosure")
    first = api.fastener_suggestions(UP)["suggestions"][0]
    api.tag_feature(first["regions"][0], "none")
    after = api.fastener_suggestions(UP)["suggestions"]
    assert len(after) == 1 and after[0]["count"] == 3 and "All three" in after[0]["prompt"]
