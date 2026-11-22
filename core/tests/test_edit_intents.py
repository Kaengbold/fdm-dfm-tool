"""Design edits say what they are in exact terms beside their triangle pieces (T-528), so the B-rep worker (T-529) can
make them on a STEP part with real faces: hole resize, teardrop, slab, void, rib and inside-corner rounds.

core/tests/step/ holds the L-bracket fixture (tools/fixtures/l_bracket.step) as the STEP importer gives it to the
engine: its triangles and the B-rep face id of each (tools/check_step_faces.mjs --dump)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from fdmcore import api

HERE = Path(__file__).parent
SAMPLES = HERE.parents[1] / "web" / "samples"
UP = [0, 0, 1]


def load_bracket():
    faces = json.loads((HERE / "step" / "l_bracket_faces.json").read_text())
    api.load((HERE / "step" / "l_bracket.stl").read_bytes(), "l_bracket", faces)
    return faces


def face_signature(faces: list[int], face: int):
    """Area and centre of one B-rep face from the importer's triangles."""
    tris = api._require().mesh.triangles[np.asarray(faces) == face]
    area = np.linalg.norm(np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]), axis=1) / 2
    return area.sum(), (tris.mean(axis=1) * area[:, None]).sum(axis=0) / area.sum()


def prism_volume(shape: dict) -> float:
    """Area of the flat outline (minus its holes) times how far it is pushed across its plane."""
    def area(ring):
        p = np.asarray(ring)
        return np.cross(p, np.roll(p, -1, axis=0)).sum(axis=0) / 2  # vector area (Newell)

    a = area(shape["outline"])
    n = a / np.linalg.norm(a)
    return (np.linalg.norm(a) - sum(np.linalg.norm(area(h)) for h in shape["holes"])) * abs(np.dot(shape["extrude"], n))


def piece_volume(edit: dict) -> float:
    from fdmcore.mesh import Mesh

    tris = np.frombuffer(edit["mesh"], "<f4").reshape(-1, 3, 3).astype(float)
    out, start = 0.0, 0
    for n in edit["pieces"]:
        out += abs(Mesh(tris[start:start + n]).volume())
        start += n
    return out


def test_the_teardrop_names_the_sideways_hole_and_its_kite():
    faces = load_bracket()
    edit = next(e for e in api.design(UP)["edits"] if e["id"].startswith("teardrop"))
    intent = edit["intent"]
    assert intent["op"] == "teardrop" and intent["params"]["diameter_mm"] == pytest.approx(8, abs=0.1)
    (face,) = intent["refs"]["faces"]
    area, centre = face_signature(faces, face)
    assert area == pytest.approx(np.pi * 8 * 5, rel=0.01) and np.allclose(centre, [-27.5, 0, 28], atol=0.1)  # the Ø8 hole
    (kite,) = intent["shapes"]
    assert kite["kind"] == "prism" and not kite["add"] and len(kite["outline"]) == 5
    assert prism_volume(kite) == pytest.approx(piece_volume(edit), rel=1e-3)
    json.dumps(intent)


def test_a_hole_resized_for_a_fit_is_one_ring_on_its_face():
    faces = load_bracket()
    hole = next(r for r in api.regions()["regions"] if r.get("hole_diameter") and abs(r["hole_diameter"] - 6) < 0.2)
    api.set_fit(hole["id"], UP, partner_mm=6.5, fit="loose")
    edit = next(e for e in api.design(UP)["edits"] if e["id"].startswith("fit-"))
    intent = edit["intent"]
    assert intent["op"] == "hole_resize" and intent["params"]["to_mm"] > 6.5
    assert intent["refs"]["faces"] == [hole["face"]] and face_signature(faces, hole["face"])[0] == pytest.approx(np.pi * 6 * 5, rel=0.01)
    (ring,) = intent["shapes"]
    assert ring["kind"] == "ring" and ring["inner"] == 0 and ring["outer"] == pytest.approx(intent["params"]["to_mm"] / 2, abs=1e-3)
    assert np.allclose(np.abs(ring["axis"]), [0, 0, 1]) and ring["to"] - ring["from"] == pytest.approx(5, abs=0.05)


def test_blocks_added_or_cut_are_a_slab_and_a_void():
    load_bracket()
    boxes = [{"kind": "add_solid", "lo": [-20, -10, 5], "hi": [20, 10, 7]}, {"kind": "cut_solid", "lo": [0, -5, 1], "hi": [10, 5, 3]}]
    slab, void = api.volume_solid(UP, boxes)
    assert slab["intent"]["op"] == "slab" and void["intent"]["op"] == "void"
    for e, add in ((slab, True), (void, False)):
        (shape,) = e["intent"]["shapes"]
        assert shape["add"] is add and prism_volume(shape) == pytest.approx(piece_volume(e), rel=1e-6)


def test_the_inside_corner_names_the_two_faces_it_lies_between():
    """The strength check's weak spot at the bracket's inside corner: the round there names the base's top face and
    the upright's inner face, the two faces whose shared B-rep edge is that corner (T-527)."""
    faces = load_bracket()
    s = api._require()
    s.weak_spot, s.strength_verdict = ((-24.0, 0.0, 6.0), 1.0), "likely to break"
    edit = next(e for e in api.design(UP)["edits"] if e["id"] == "stress-chamfer")
    intent = edit["intent"]
    assert intent["op"] in ("fillet", "chamfer") and intent["shapes"] == []
    (edge,) = intent["refs"]["edges"]
    centres = [face_signature(faces, f)[1] for f in edge["faces"]]
    assert sorted(round(float(c[0])) for c in centres) == [-25, 2]  # the upright's inner face and the base's top
    assert all(abs(p[0] + 25) < 0.1 and abs(p[2] - 5) < 0.1 for p in edge["points"]) and edge["size"] > 0


def test_ribs_and_grown_walls_are_flat_outlines_pushed_out():
    """Mesh parts (no STEP): the intents still describe the shapes exactly, only without B-rep faces."""
    api.load((SAMPLES / "detail_plate.stl").read_bytes(), "detail_plate")
    ribs = next(e for e in api.design(UP)["edits"] if e["id"].startswith("boss-ribs"))
    assert ribs["intent"]["op"] == "rib" and len(ribs["intent"]["shapes"]) == ribs["intent"]["params"]["count"] == len(ribs["pieces"])
    assert sum(prism_volume(p) for p in ribs["intent"]["shapes"]) == pytest.approx(piece_volume(ribs), rel=1e-3)
    assert "faces" not in ribs["intent"]["refs"]
    api.load((SAMPLES / "test_block.stl").read_bytes(), "test_block")
    wall = next(e for e in api.design(UP)["edits"] if e["id"].startswith("wall-"))
    assert wall["intent"]["op"] == "slab" and len(wall["intent"]["shapes"]) == len(wall["pieces"])
    assert sum(prism_volume(p) for p in wall["intent"]["shapes"]) == pytest.approx(piece_volume(wall), rel=1e-3)
