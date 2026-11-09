"""Split a part that is too big for the printer (T-321)."""

import pathlib

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


def test_mcp_split_part_stays_in_the_allowed_folder(tmp_path, monkeypatch):
    """The agent tool writes the pieces only inside --folder, and never over the user's own files."""
    pytest.importorskip("manifold3d")
    pytest.importorskip("mcp")
    from fdmcore import mcp_server as m

    api.load(to_binary_stl(Mesh(box([0, 0, 0], 1) * [400, 40, 20])), "bar")
    monkeypatch.setenv("FDM_DFM_FOLDER", str(tmp_path / "jail"))
    with pytest.raises(ValueError, match="only use files inside"):
        m.split_part(str(tmp_path / "out"))
    (tmp_path / "jail").mkdir()
    (tmp_path / "jail" / "pin.stl").write_bytes(b"the user's own file")
    with pytest.raises(ValueError, match="already exists"):
        m.split_part(str(tmp_path / "jail"))
    got = m.split_part(str(tmp_path / "jail" / "pieces"))
    assert len(got["saved"]) == 3 and all(pathlib.Path(p).stat().st_size > 84 for p in got["saved"])


def test_pin_holes_cut_for_every_cut_direction():
    """The pin holes are cut out whichever axis the part is cut across (a mirrored set of axes once
    turned them inside out, so pieces cut across y got no holes)."""
    pytest.importorskip("manifold3d")
    for size in ([400, 40, 20], [40, 400, 20], [40, 30, 400]):
        part = Mesh(box([0, 0, 0], 1) * size)
        plan = split_plan(part, BED)
        holes = 2 * 2 * np.pi * (plan["pin"]["hole_mm"] / 2) ** 2 * 6
        assert sum(abs(p.volume()) for p in _pieces(part, plan)) == pytest.approx(np.prod(size) - holes, rel=1e-3), size


def test_dovetail_joins_the_pieces():
    """A sliding dovetail instead of pins: the piece below keeps a flared tongue (wider at its tip, so it
    can't be pulled straight out), the piece above the groove with the fit gap; nothing else is lost, and
    each piece says how to turn it so it fits the bed."""
    pytest.importorskip("manifold3d")
    from fdmcore.slicer import section

    part = Mesh(box([0, 0, 0], 1) * [400, 40, 20])
    plan = split_plan(part, BED, joint="dovetail")
    assert plan["joint"] == "dovetail" and plan["pin"]["count"] == 0 and "dovetail" in plan["explanation"]
    low, high = _pieces(part, plan)
    c = plan["cuts"][0]
    assert np.ptp(low.triangles.reshape(-1, 3)[:, 0]) > c + 2  # the tongue sticks out past the cut
    along = low.triangles[..., [1, 2, 0]]  # cut axis (x) as height for section()
    assert section(along, c + 0.5).area < section(along, c + 5).area  # flared: narrow at the cut, wider further out
    lost = 400 * 40 * 20 - abs(low.volume()) - abs(high.volume())
    assert 0 < lost < 0.002 * 400 * 40 * 20  # just the gap round the tongue
    for p, info in zip((low, high), plan["pieces"]):
        assert _clean(p.triangles) == (0, 0)
        turned = np.abs(np.array(info["rotation"]) @ np.ptp(p.triangles.reshape(-1, 3), axis=0))
        assert np.all(turned <= np.array(BED) + 1e-3) and turned[2] == pytest.approx(20, abs=0.1)  # lying flat


def test_dovetail_needs_room():
    plan = split_plan(Mesh(box([0, 0, 0], 1) * [400, 12, 12]), BED, joint="dovetail")
    assert plan["joint"] == "pins" and "no room for a dovetail" in plan["explanation"]
