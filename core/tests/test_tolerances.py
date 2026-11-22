"""Tolerances & fits (T-570): api.round_features lists the holes and pegs a fit can be set on, with the regions of
the same size and the size each prints at (measured when the user ran the fit test, typical otherwise)."""

from __future__ import annotations

from pathlib import Path

import pytest

from fdmcore import api

SAMPLES = Path(__file__).resolve().parents[2] / "web" / "samples"
PROFILE = {"printer": "bambu_p1s", "material": "pla"}


def _load(name: str):
    api.load((SAMPLES / f"{name}.stl").read_bytes(), name)


def test_holes_and_pegs_with_their_twins_and_printed_size():
    _load("enclosure")
    out = api.round_features([0, 0, 1], PROFILE)
    by_kind = {k: [f for f in out["features"] if f["kind"] == k] for k in ("hole", "peg")}
    assert len(by_kind["hole"]) == 4 and len(by_kind["peg"]) == 4
    for f in out["features"]:
        assert sorted(f["same"]) == sorted(g["region"] for g in by_kind[f["kind"]])
        assert f["printed_from"] == "typical" and f["fit"] is None and f["fastener"] is None
    hole = by_kind["hole"][0]
    assert hole["diameter"] == pytest.approx(2.8) and hole["printed_mm"] < hole["diameter"], "small holes print undersized"
    assert by_kind["peg"][0]["printed_mm"] == by_kind["peg"][0]["diameter"]
    assert out["tolerance"]["gaps"]["snug"] > out["tolerance"]["gaps"]["press"]
    assert api.dispatch("round_features", '{"up": [0, 0, 1], "profile": {"printer": "bambu_p1s"}}')["features"]


def test_what_is_set_shows_and_measured_errors_win():
    _load("enclosure")
    hole = next(f for f in api.round_features([0, 0, 1], PROFILE)["features"] if f["kind"] == "hole")
    api.set_fastener(hole["region"], [0, 0, 1], "M3", "insert", PROFILE)
    again = next(f for f in api.round_features([0, 0, 1], PROFILE)["features"] if f["region"] == hole["region"])
    assert again["fastener"] == {"size": "M3", "use": "insert"}
    # The user's own filament with a measured hole error (Settings & calibration).
    api.custom_presets(materials={"my_pla": {"based_on": "pla", "name": "My PLA", "hole_error_mm": -0.1}})
    try:
        mine = next(f for f in api.round_features([0, 0, 1], {**PROFILE, "material": "my_pla"})["features"] if f["region"] == hole["region"])
        assert mine["printed_from"] == "measured" and mine["printed_mm"] == pytest.approx(2.7)
    finally:
        api.custom_presets()
