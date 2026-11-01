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
