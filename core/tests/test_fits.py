import numpy as np
import pytest
from shapely.geometry import Point, box

from fdmcore import api
from fdmcore.additions import extrude
from fdmcore.mesh import Mesh, to_binary_stl


def plate_with_hole():
    """30 × 20 × 5 mm plate with an upright Ø6 hole."""
    api.load(to_binary_stl(Mesh(extrude(box(0, 0, 30, 20).difference(Point(8, 10).buffer(3, 64)), 0, 5))), "plate")
    return next(f for f in api.design([0, 0, 1], {})["features"] if f["type"] == "hole")


def peg():
    """A Ø5 × 8 mm round peg."""
    api.load(to_binary_stl(Mesh(extrude(Point(0, 0).buffer(2.5, 64), 0, 8))), "peg")
    return next(f for f in api.design([0, 0, 1], {})["features"] if f["type"] == "boss")


def test_hole_and_post_sizes_for_each_fit():
    hole = plate_with_hole()
    slide = api.set_fit(hole["region"], [0, 0, 1], 6.0, "sliding")
    press = api.set_fit(hole["region"], [0, 0, 1], 6.0, "press")
    assert slide["suggested_mm"] == pytest.approx(6.35) and press["suggested_mm"] == pytest.approx(6.1)
    assert "slide" in slide["explanation"]
    sideways = api.set_fit(hole["region"], [1, 0, 0], 6.0, "sliding")  # printed lying down: a bit more room
    assert sideways["suggested_mm"] == pytest.approx(6.45)
    with pytest.raises(ValueError, match="fit must be"):
        api.set_fit(hole["region"], [0, 0, 1], 6.0, "tight")
    post = peg()
    assert api.set_fit(post["region"], [0, 0, 1], 5.0, "snug")["suggested_mm"] == pytest.approx(4.8)


def test_resize_edits_cut_or_add_the_right_ring():
    hole = plate_with_hole()
    api.set_fit(hole["region"], [0, 0, 1], 6.0, "sliding")  # Ø6 hole -> Ø6.35: cut
    cut = next(e for e in api.design([0, 0, 1], {})["edits"] if e["id"] == f"fit-{hole['region']}")
    assert cut["op"] == "cut"
    ring = np.frombuffer(cut["mesh"], "<f4").reshape(-1, 3, 3)
    r = np.linalg.norm(ring[..., :2].reshape(-1, 2) - [8, 10], axis=1)
    assert r.max() == pytest.approx(3.175, abs=0.01)  # the new hole's radius
    api.set_fit(hole["region"], [0, 0, 1], None)
    assert f"fit-{hole['region']}" not in {e["id"] for e in api.design([0, 0, 1], {})["edits"]}
    post = peg()
    api.set_fit(post["region"], [0, 0, 1], 5.5, "sliding")  # Ø5 post for a Ø5.5 hole -> Ø5.15: add a ring
    add = next(e for e in api.design([0, 0, 1], {})["edits"] if e["id"] == f"fit-{post['region']}")
    ring = np.frombuffer(add["mesh"], "<f4").reshape(-1, 3, 3)
    assert add["op"] == "add" and np.linalg.norm(ring[..., :2].reshape(-1, 2), axis=1).max() == pytest.approx(2.575, abs=0.01)


def test_only_round_features_take_a_fit():
    plate_with_hole()
    flat = max(api.regions()["regions"], key=lambda r: r["normal"][2])["id"]
    with pytest.raises(ValueError, match="not a round hole"):
        api.set_fit(flat, [0, 0, 1], 6.0)
