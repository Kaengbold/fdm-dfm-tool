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


def _spool():
    """Two 40 × 40 plates joined by a 10 × 10 post, 50 mm long: every way up, one plate overhangs (on its side,
    the post spans more than a bridge can)."""
    m3 = pytest.importorskip("manifold3d")
    box_ = lambda lo, size: m3.Manifold.cube(size).translate(lo)  # noqa: E731
    mm = (box_((0, 0, 0), (40, 40, 5)) + box_((15, 15, 5), (10, 10, 50)) + box_((0, 0, 55), (40, 40, 5))).to_mesh()
    v, f = np.asarray(mm.vert_properties)[:, :3], np.asarray(mm.tri_verts)
    return Mesh(v[f])


def test_cut_to_print_without_supports():
    """A part that fits but needs support every way up: cut half way up its post, each piece prints its own way
    (plate down) without support; the pin holes stay inside both pieces. Parts that print fine aren't cut."""
    api.load(to_binary_stl(_spool()), "spool")
    plan = api.split({}, purpose="supports")
    assert plan["axis"] == 2 and plan["cuts"] == [pytest.approx(30, abs=4)]
    assert plan["support_mm3"]["pieces"] < 0.5 * plan["support_mm3"]["whole"] and "much less support" in plan["explanation"]
    ups = [np.array(p["rotation"])[2] for p in plan["pieces"]]  # each piece: its plate on the bed
    assert ups[0] @ [0, 0, 1] > 0.99 and ups[1] @ [0, 0, -1] > 0.99
    for p in _pieces(api._require().mesh, plan):
        assert _clean(p.triangles) == (0, 0)
    api.load(to_binary_stl(Mesh(box([0, 0, 0], 1) * [40, 30, 20])), "block")
    with pytest.raises(ValueError, match="one piece"):
        api.split({}, purpose="supports")


def _three_arms(block: float):
    """Three 60 mm arms along x, y and z from one corner (meeting in a ``block`` mm cube), held at the x arm's end,
    pushed at the y and z arms' ends: no single way to lay it has every arm's layers along its own load."""
    trimesh = pytest.importorskip("trimesh")
    pytest.importorskip("manifold3d")
    L, T = 60, 8
    boxes = [trimesh.creation.box(extents=(block, block, block)).apply_translation((block / 2,) * 3)] + [
        trimesh.creation.box(extents=e).apply_translation(c) for e, c in (
            ((L, T, T), (L / 2, block / 2, block / 2)), ((T, L, T), (block / 2, L / 2, block / 2)), ((T, T, L), (block / 2, block / 2, L / 2)))]
    api.load(to_binary_stl(Mesh(trimesh.boolean.union(boxes, engine="manifold").triangles)), "arms")
    api.regions()
    s = api._require()
    normals, _ = s.mesh.normals_and_areas()
    c = s.mesh.triangles.mean(axis=1)
    face = lambda k: int(s.regions[np.flatnonzero((normals[:, k] > 0.99) & (np.abs(c[:, k] - L) < 1e-3))[0]])  # noqa: E731
    return [{"region": face(0), "kind": "fixed"}, {"region": face(1), "kind": "force", "kg": 3, "direction": [0, 0, -1]},
            {"region": face(2), "kind": "force", "kg": 3, "direction": [0, -1, 0]}]


def test_cut_so_each_piece_prints_its_strong_way():
    """T-347: with a solid block where the arms meet, cutting off one arm lets each piece lie its strong way: clearly
    stronger together, the glued face inside the block where little load crosses it. Offered as an idea too."""
    loads = _three_arms(20)
    plan = api.split({}, "pins", "strength", loads)
    sf = plan["safety"]
    assert sf["safety"] >= 1.25 * sf["whole_safety"] and sf["joint_safety"] > 2 * sf["safety"]
    assert len(plan["pieces"]) == 2 and "layers along its own load" in plan["explanation"]
    ideas = api.strength_options([0, 0, 1], loads, {})  # "How can I make it stronger?"
    assert any(o["kind"] == "split" and o["purpose"] == "strength" and o["safety_factor"] > o.get("base_safety", 0) for o in ideas)


def test_no_strength_cut_where_one_orientation_suits_it():
    """Arms meeting at a bare corner are limited at the corner itself, which no cut helps: not cut. No loads: no cut."""
    loads = _three_arms(8)
    with pytest.raises(ValueError, match="one piece"):
        api.split({}, "pins", "strength", loads)
    with pytest.raises(ValueError, match="load"):
        api.split({}, "pins", "strength", None)
