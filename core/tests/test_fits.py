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


def test_measured_gaps_from_the_fit_test_print_are_used():
    hole = plate_with_hole()
    api.custom_presets(materials={"mine": {"based_on": "pla", "name": "My PLA", "fit_gap_mm": {"sliding": 0.25}}})
    try:
        r = api.set_fit(hole["region"], [0, 0, 1], 6.0, "sliding", {"material": "my_mine"})
        assert r["suggested_mm"] == pytest.approx(6.25) and "fit test" in r["explanation"]
        assert api.set_fit(hole["region"], [0, 0, 1], 6.0, "snug", {"material": "my_mine"})["suggested_mm"] == pytest.approx(6.2)  # not measured: default
    finally:
        api.custom_presets()


def test_calibration_prints():
    from fdmcore.mesh import load_stl

    for kind, size in (("fit", (64, 28, 12)), ("overhang", (49.2, 80, 15)), ("bridge", (60, 64, 11.2))):
        m = load_stl(api.calibration_part(kind)["stl"])
        lo, hi = m.bounds()
        assert np.allclose(hi - lo, size, atol=0.2) and m.volume() > 0, kind


def slotted_block():
    """A 30 × 20 × 10 mm block with a 4 mm wide slot across it (x 13–17, from the top down to 3 mm)."""
    shape = box(0, 0, 30, 10).difference(box(13, 3, 17, 11))  # profile in x-z, 20 mm deep along y
    t = extrude(shape, 0, 20)[..., [0, 2, 1]][:, [0, 2, 1]]  # (x, z, depth) -> (x, depth, z), flipped back outward
    api.load(to_binary_stl(Mesh(t)), "slotted")
    api.regions()
    s = api._require()
    normals, _ = s.mesh.normals_and_areas()
    c = s.mesh.triangles.mean(axis=1)
    face = lambda n, x: int(s.regions[np.flatnonzero((normals @ n > 0.99) & (np.abs(c[:, 0] - x) < 1e-3))[0]])
    return face([1, 0, 0], 13.0), face([-1, 0, 0], 17.0), face([-1, 0, 0], 0.0), face([1, 0, 0], 30.0)


def test_slot_and_tab_between_flat_faces():
    a, b, left, right = slotted_block()
    m = api.set_gap(a, b, [0, 0, 1])
    assert m["kind"] == "slot" and m["distance_mm"] == pytest.approx(4.0)
    r = api.set_gap(a, b, [0, 0, 1], 4.0, "sliding")
    assert r["suggested_mm"] == pytest.approx(4.35) and "slot" in r["explanation"]
    edit = next(e for e in api.design([0, 0, 1], {})["edits"] if e["id"] == f"gap-{min(a, b)}-{max(a, b)}")
    assert edit["op"] == "cut"
    pts = np.frombuffer(edit["mesh"], "<f4").reshape(-1, 3)
    assert pts[:, 0].min() == pytest.approx(13 - 0.175, abs=0.01) and pts[:, 0].max() == pytest.approx(17 + 0.175, abs=0.01)
    whole = api.set_gap(left, right, [0, 0, 1])
    assert whole["kind"] == "tab" and whole["distance_mm"] == pytest.approx(30.0)
    assert api.set_gap(left, right, [0, 0, 1], 31.0, "snug")["suggested_mm"] == pytest.approx(30.8)
    api.set_gap(a, b, [0, 0, 1])  # measuring only forgets the size
    assert not any(e["id"].startswith(f"gap-{min(a, b)}") for e in api.design([0, 0, 1], {})["edits"])
    with pytest.raises(ValueError, match="parallel"):
        api.set_gap(a, right, [0, 0, 1])  # both face +x


def test_flat_gap_across_layers_and_warping_tightens():
    a, b, _, _ = slotted_block()
    lying = api.set_gap(a, b, [1, 0, 0], 4.0, "sliding")  # the slot's faces lie flat: across the layers
    assert lying["suggested_mm"] == pytest.approx(4.45) and "across the layers" in lying["explanation"]
    api.warping([0, 0, 1], {"material": "abs", "printer": "prusa_mk4s"})
    r = api.set_gap(a, b, [0, 0, 1], 4.0, "sliding")
    assert r["warp_mm"] >= 0 and r["suggested_mm"] == pytest.approx(4.35 + r["warp_mm"], abs=0.005)
