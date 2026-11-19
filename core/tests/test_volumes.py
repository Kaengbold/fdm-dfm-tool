import io
import zipfile

import numpy as np
import pytest

from fdmcore import api

from test_hold import box, region_facing
from test_zones import cantilever


def test_pin_hole_stays_inside_and_adds_strength():
    loads = cantilever()
    v = api.volume_at([0, 0, 1], "void", point=[10, 6, 6])
    assert v["hi"][2] - v["lo"][2] == pytest.approx(12 - 2 * api.VOID_COVER_MM, abs=0.3)  # up through the bar, covered
    assert v["hi"][0] - v["lo"][0] == pytest.approx(api.VOID_MM)
    assert not any("outside" in n for n in v["notes"])
    plain = api.strength([0, 0, 1], loads, walls=2, infill=0.1)["safety_factor"]
    # A row of pin holes across the bar next to the clamp: walls round each make it solid there.
    pins = [api.volume_at([0, 0, 1], "void", point=[x, y, 6]) for x in (3, 6, 9) for y in (3, 6, 9)]
    holed = api.strength([0, 0, 1], loads, walls=2, infill=0.1, zones=pins)["safety_factor"]
    assert holed > plain


def test_gap_reaching_the_surface_is_flagged():
    api.load(box(40, 20, 10), "block")
    v = api.volume_at([0, 0, 1], "void", point=[2, 2, 5], point2=[10, 10, 5], margin=4)
    assert any("shows as a hole" in n for n in v["notes"])


def test_keep_clear_measures_the_part_inside():
    api.load(box(40, 20, 10), "block")
    empty = api.volume_at([0, 0, 1], "keep_clear", point=[0, 0, 20], point2=[40, 20, 30], margin=0)
    assert empty["lo"][2] == pytest.approx(20) and empty["hi"][2] == pytest.approx(30)  # above the part is fine
    assert "stays out" in empty["notes"][0]
    half = api.volume_at([0, 0, 1], "keep_clear", point=[0, 0, 0], point2=[20, 20, 10], margin=0)
    assert "fills about" in half["notes"][0]


def test_blocker_warns_about_overhangs_and_export_types():
    api.load(box(40, 20, 10), "block")
    up_side_down = [0, 0, -1]  # the box's bottom face... is on the bed either way: no overhang
    b = api.volume_at(up_side_down, "blocker", region=region_facing([0, 0, -1]))
    assert b["notes"] == ["No support is printed here."]
    s = api.volume_at([0, 0, 1], "support", point=[20, 10, 10])
    vols = [s, b, api.volume_at([0, 0, 1], "void", point=[20, 10, 5]), api.volume_at([0, 0, 1], "keep_clear", point=[5, 5, 5])]
    orca = zipfile.ZipFile(io.BytesIO(api.export([0, 0, 1], {}, {"wall_loops": "2"}, zones=vols, slicer="orca")))
    conf = orca.read("Metadata/model_settings.config").decode()
    assert all(f'subtype="{t}"' in conf for t in ("support_enforcer", "support_blocker", "negative_part"))
    assert conf.count("<part ") == 4  # the part and three boxes: keep clear isn't exported
    assert 'key="enable_support" value="1"' in conf and "tree(manual)" in conf
    prusa = zipfile.ZipFile(io.BytesIO(api.export([0, 0, 1], {}, {"wall_loops": "2"}, zones=vols, slicer="prusa")))
    conf = prusa.read("Metadata/Slic3r_PE_model.config").decode()
    assert all(f'value="{t}"' in conf for t in ("SupportEnforcer", "SupportBlocker", "NegativeVolume"))
    assert 'key="support_material_auto" value="0"' in conf


def test_unknown_kind():
    api.load(box(40, 20, 10), "block")
    with pytest.raises(ValueError, match="Unknown kind"):
        api.volume_at([0, 0, 1], "glue", point=[0, 0, 0])


def test_add_and_cut_blocks_change_the_model():
    from fdmcore.booleans import apply_edits

    api.load(box(40, 20, 10), "block")
    add = api.volume_at([0, 0, 1], "add_solid", point=[40, 10, 5], margin=3)  # 6 mm cube, half outside the end
    cut = api.volume_at([0, 0, 1], "cut_solid", point=[5, 10, 10], margin=2)  # 4 mm cube at the top edge, half inside
    assert "Adds about 0.1 cm³" in add["notes"][0] and "Cuts away about 0.0 cm³" in cut["notes"][0]
    far = api.volume_at([0, 0, 1], "add_solid", point=[80, 10, 5], margin=3)
    assert "doesn't touch" in far["notes"][0]
    edits = api.volume_solid([0, 0, 1], [add, cut, {"kind": "support", "lo": [0, 0, 0], "hi": [1, 1, 1]}])
    assert [e["op"] for e in edits] == ["add", "cut"]
    part = api._require().mesh
    grown = apply_edits(part, [edits[0]])
    assert abs(grown.volume()) - abs(part.volume()) == pytest.approx(3 * 6 * 6, rel=0.02)
    assert abs(part.volume()) - abs(apply_edits(part, [edits[1]]).volume()) == pytest.approx(4 * 4 * 2, rel=0.05)
    z = zipfile.ZipFile(io.BytesIO(api.export([0, 0, 1], {}, {"wall_loops": "2"}, zones=[add, cut])))
    assert "Metadata/Slic3r_PE_model.config" in z.namelist()  # nothing for the slicer: the plain single-file 3MF


def test_snap_another_model_onto_a_face():
    from fdmcore.mesh import to_binary_stl, Mesh

    api.load(box(40, 20, 10), "block")
    cube = Mesh(_cube(6))
    e = api.snap_model(to_binary_stl(cube), point=[20, 10, 10], normal=[0, 0, 1], name="cube")
    tris = np.frombuffer(e["mesh"], "<f4").reshape(-1, 3)
    assert e["op"] == "add" and tris[:, 2].min() == pytest.approx(10 - api.SNAP_SINK_MM, abs=1e-4)
    assert tris[:, 2].max() == pytest.approx(16 - api.SNAP_SINK_MM, abs=1e-4)
    assert tris[:, 0].mean() == pytest.approx(20, abs=0.5) and tris[:, 1].mean() == pytest.approx(10, abs=0.5)
    side = api.snap_model(to_binary_stl(cube), point=[40, 10, 5], normal=[1, 0, 0], spin_deg=45)
    pts = np.frombuffer(side["mesh"], "<f4").reshape(-1, 3)
    assert pts[:, 0].min() == pytest.approx(40 - api.SNAP_SINK_MM, abs=1e-4) and pts[:, 1].max() - pts[:, 1].min() == pytest.approx(6 * np.sqrt(2), abs=0.01)


def _cube(size):
    v = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], float) * size
    f = [[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]]
    return v[np.array(f)]


def test_suggestions_keep_out_of_keep_clear_space():
    api.load(box(40, 20, 10), "block")
    block = api.volume_at([0, 0, 1], "add_solid", point=[40, 10, 5], margin=3)
    edits = api.volume_solid([0, 0, 1], [block])
    clear = api.volume_at([0, 0, 1], "keep_clear", point=[44, 10, 5], margin=1)  # just past the end, where the block goes
    elsewhere = api.volume_at([0, 0, 1], "keep_clear", point=[0, 10, 20], margin=1)
    assert "clash" in api._keep_clear_clash([dict(e) for e in edits], [0, 0, 1], [clear])[0]
    assert "clash" not in api._keep_clear_clash([dict(e) for e in edits], [0, 0, 1], [elsewhere])[0]
    cut = api.volume_solid([0, 0, 1], [{**block, "kind": "cut_solid"}])
    assert "clash" not in api._keep_clear_clash(cut, [0, 0, 1], [clear])[0]  # cutting never fills the space
    assert "edits" in api.design([0, 0, 1], {}, keep_clear=[clear])


def test_support_boxes_cover_only_the_picked_face():
    """T-363: "No support here" on the mushroom cap's underside covers that face (not 3 mm past it all
    round) and reaches down to the bed, where the supports under it stand; on the top face, up to the top."""
    from pathlib import Path

    api.load((Path(__file__).resolve().parents[2] / "web" / "samples" / "mushroom.stl").read_bytes(), "mushroom")
    regs = api.regions()["regions"]
    under = next(r["id"] for r in regs if r["normal"][2] < -0.9 and r["centroid"][2] > 10)  # the cap's underside, z = 20
    v = api.volume_at([0, 0, 1], "blocker", region=under)
    assert v["margin"] == pytest.approx(api.FACE_SUPPORT_MARGIN_MM)
    assert v["lo"][:2] == pytest.approx([-20.5, -20.5], abs=0.01) and v["hi"][:2] == pytest.approx([20.5, 20.5], abs=0.01)
    assert v["lo"][2] == pytest.approx(0.0, abs=0.01) and v["hi"][2] == pytest.approx(20.5, abs=0.01)
    top = next(r["id"] for r in regs if r["normal"][2] > 0.9)
    w = api.volume_at([0, 0, 1], "support", region=top)
    assert w["lo"][2] == pytest.approx(25.5, abs=0.01) and w["hi"][2] == pytest.approx(26.0, abs=0.01)  # the part's top: nothing above


def test_pin_hole_from_a_side_face_goes_into_the_wall():
    """T-363: a click on a side face is on its surface; the upright pin hole moves into the wall."""
    api.load(box(40, 20, 10), "block")
    v = api.volume_at([0, 0, 1], "void", point=[0.0, 10, 5], normal=[-1, 0, 0])  # the x = 0 side
    middle = (v["lo"][0] + v["hi"][0]) / 2
    assert middle == pytest.approx(api.VOID_COVER_MM + api.VOID_MM / 2, abs=0.01)
    assert v["hi"][2] - v["lo"][2] == pytest.approx(10 - 2 * api.VOID_COVER_MM, abs=0.3)
    assert not any("shows as a hole" in n for n in v["notes"])


def test_moved_box_keeps_its_size_and_stays_on_the_part():
    """T-392: a box dragged in the 3D view. Moved in part coordinates (here the part lies on its side,
    so part +z is a sideways move on the bed), same size, its notes updated, its height still within the part."""
    api.load(box(40, 20, 10), "block")
    up = [0, -1, 0]  # part -y points up: part z runs along the bed
    v = api.volume_at(up, "support", point=[10, 10, 5], margin=2)
    moved = api.volume_move(up, v, [0, 0, 3])
    size = np.subtract(v["hi"], v["lo"])
    assert np.subtract(moved["hi"], moved["lo"]) == pytest.approx(size)
    shift = np.subtract(moved["lo"], v["lo"])
    assert np.linalg.norm(shift) == pytest.approx(3) and abs(shift[2]) < 1e-9  # sideways on the bed, not up
    assert moved["margin"] == v["margin"] and moved["notes"] and len(moved["corners_print"]) == 8
    up_high = api.volume_move(up, v, [0, -9, 0])  # dragged 9 mm up, past the top: cut off there
    assert up_high["hi"][2] - up_high["lo"][2] == pytest.approx(size[2] - 9 + 8, abs=1e-6)  # 4 mm tall, its top 8 mm under the part's: 1 mm cut off
    with pytest.raises(ValueError, match="outside the part's height"):
        api.volume_move(up, v, [0, -200, 0])


def test_pin_hole_on_vertical_wall_without_normal():
    """T-485: a click on a vertical wall (flat or curved) without a face normal, or with a curved
    region's near-zero average normal, steps into the wall using the local surface normal."""
    from pathlib import Path

    # Simple box: vertical side wall with no normal supplied, and rotated orientation.
    api.load(box(40, 20, 10), "block")
    v = api.volume_at([0, 0, 1], "void", point=[0.0, 10, 5])
    assert (v["lo"][0] + v["hi"][0]) / 2 == pytest.approx(api.VOID_COVER_MM + api.VOID_MM / 2, abs=0.01)
    assert not any("shows as a hole" in n for n in v["notes"])
    rot = api.volume_at([1, 0, 0], "void", point=[20, 10, 0.0])  # part z = 0 is a vertical wall when +x is up
    assert rot["hi"][2] - rot["lo"][2] == pytest.approx(40 - 2 * api.VOID_COVER_MM, abs=0.3)
    assert not any("shows as a hole" in n for n in rot["notes"])
    with pytest.raises(ValueError, match="isn't on the part"):
        api.volume_at([0, 0, 1], "void", point=[100, 100, 100])

    # Thin vertical wall: stepping inward crosses the wall, so report it as too thin rather than off-part.
    api.load(box(1.0, 20, 10), "fin")
    with pytest.raises(ValueError, match="too thin"):
        api.volume_at([0, 0, 1], "void", point=[0.0, 10, 5])

    # spool_hub.stl: first triangle lies on the outer cylindrical flange (reproduces tools/pyodide_check.mjs).
    api.load((Path(__file__).resolve().parent / "corpus" / "spool_hub.stl").read_bytes(), "spool_hub.stl")
    up = api.orientations({})["orientations"][0]["up"]
    c = api._require().mesh.triangles[0].mean(axis=0).tolist()
    hub = api.volume_at(up, "void", point=c, profile={})
    assert hub["hi"][2] > hub["lo"][2] and not any("shows as a hole" in n for n in hub["notes"])
    # Curved region's average normal is near zero (as web/app.js passes); local surface normal still wins.
    reg = api.regions()
    hub_web = api.volume_at(up, "void", point=c, normal=reg["regions"][reg["face_regions"][0]]["normal"], profile={})
    assert hub_web["lo"] == pytest.approx(hub["lo"]) and hub_web["hi"] == pytest.approx(hub["hi"])
