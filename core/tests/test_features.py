from pathlib import Path

import numpy as np
import pytest

from fdmcore import api
from fdmcore.mesh import Mesh

SAMPLES = Path(__file__).resolve().parents[2] / "web" / "samples"


def design(name, up, **profile):
    api.load((SAMPLES / f"{name}.stl").read_bytes(), name)
    return api.design(up, profile)


def test_holes_detected():
    r = design("l_bracket", [0, 0, 1])
    holes = sorted((f["diameter"], round(abs(f["axis"][2]))) for f in r["features"] if f["type"] == "hole")
    assert holes == [(pytest.approx(6.0, abs=0.05), 1), (pytest.approx(8.0, abs=0.05), 0)]


def test_teardrop_only_for_horizontal_holes():
    lying = design("l_bracket", [0, 0, 1])["edits"]
    assert [e["id"] for e in lying if e["id"].startswith("teardrop")] == ["teardrop-8"]
    # Printing with X up turns the Ø8 hole vertical and the Ø6 hole horizontal.
    upright = design("l_bracket", [1, 0, 0])["edits"]
    assert [e["title"] for e in upright if e["id"].startswith("teardrop")] == ["Teardrop for the horizontal Ø6.0 mm hole"]


def test_cutters_are_closed_and_outward():
    for e in design("l_bracket", [0, 0, 1], material="abs")["edits"]:
        tris = np.frombuffer(e["cutter"], "<f4").reshape(-1, 3, 3).astype(float)
        assert Mesh(tris).volume() > 0, e["id"]


def test_corner_chamfers_only_for_warping_materials():
    assert not any(e["rule"] == "sharp_bed_corners" for e in design("l_bracket", [0, 0, 1], material="pla")["edits"])
    assert sum(e["rule"] == "sharp_bed_corners" for e in design("l_bracket", [0, 0, 1], material="abs")["edits"]) == 4


def test_edit_params_change_cutters():
    from pathlib import Path

    import numpy as np

    from fdmcore import api

    api.load((Path(__file__).resolve().parents[2] / "web" / "samples" / "l_bracket.stl").read_bytes(), "l")
    prof = {"material": "abs"}

    def apex(angle):
        r = api.design([0, 0, 1], prof, {"teardrop_angle_deg": angle})
        tear = next(e for e in r["edits"] if e["id"].startswith("teardrop"))
        return np.frombuffer(tear["cutter"], "<f4").reshape(-1, 3)[:, 2].max(), r

    pointy, r30 = apex(30)
    classic, _ = apex(45)
    assert pointy > classic  # a narrower tip sits higher: r/sin(30°) = 2r vs 1.41r
    assert r30["params"]["teardrop_angle_deg"] == 30
    chamfers = [e["title"] for e in api.design([0, 0, 1], prof, {"chamfer_mm": 1.5})["edits"] if e["id"].startswith("chamfer")]
    assert chamfers and all("1.5 mm" in t for t in chamfers)
    assert api.design([0, 0, 1], prof, {"chamfer_mm": 99})["params"]["chamfer_mm"] == 10  # clamped
