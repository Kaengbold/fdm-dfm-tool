"""Split a part that is too big for the printer (T-321)."""

import numpy as np
import pytest

from fdmcore import api
from fdmcore.mesh import Mesh, to_binary_stl
from fdmcore.split import split_plan

from test_features import _clean
from test_formats import box

BED = [256, 256, 256]


def _pieces(part, plan):
    from fdmcore.booleans import apply_edits

    return [apply_edits(part, [{**e, "mesh": np.frombuffer(e["mesh"], "<f4").reshape(-1, 3, 3)} for e in p["edits"]])
            for p in plan["pieces"]]


def test_long_bar_in_two_pieces_with_pins():
    pytest.importorskip("manifold3d")
    part = Mesh(box([0, 0, 0], 1) * [400, 40, 20])
    plan = split_plan(part, BED)
    assert plan["axis"] == 0 and plan["cuts"] == [pytest.approx(200, abs=50)]
    assert plan["pin"]["count"] == 2 and "pins" in plan["explanation"]
    pieces = _pieces(part, plan)
    holes = 2 * 2 * np.pi * (plan["pin"]["hole_mm"] / 2) ** 2 * 6  # two pins, a hole on each side
    assert sum(abs(p.volume()) for p in pieces) == pytest.approx(400 * 40 * 20 - holes, rel=1e-3)
    for p in pieces:
        assert _clean(p.triangles) == (0, 0)
        size = np.ptp(p.triangles.reshape(-1, 3), axis=0)
        assert np.all(np.sort(size) <= np.sort(BED))


def test_very_long_part_gets_more_cuts_and_avoids_the_weak_spot():
    part = Mesh(box([0, 0, 0], 1) * [700, 30, 30])
    plan = split_plan(part, BED)
    assert len(plan["pieces"]) == 3
    weak = split_plan(Mesh(box([0, 0, 0], 1) * [400, 40, 20]), BED, weak_spot=[200, 20, 10])
    assert abs(weak["cuts"][0] - 200) > 20  # moved off the weak spot


def test_thin_cut_face_is_glued_without_pins():
    plan = split_plan(Mesh(box([0, 0, 0], 1) * [400, 40, 3]), BED)
    assert plan["pin"]["count"] == 0 and "glue" in plan["explanation"]


def test_part_that_fits_is_not_split():
    api.load(to_binary_stl(Mesh(box([0, 0, 0], 50))), "cube")
    with pytest.raises(ValueError, match="already fits"):
        api.split({})
