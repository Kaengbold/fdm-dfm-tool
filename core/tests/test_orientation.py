from pathlib import Path

import numpy as np
import pytest

from fdmcore import api
from fdmcore.mesh import Mesh, load_stl, to_binary_stl
from fdmcore.orientation import BED, NEEDS_SUPPORT, OK, classify_faces, find_orientations, rotation_to_z

SAMPLES = Path(__file__).resolve().parents[2] / "web" / "samples"


def cube(size=10.0) -> Mesh:
    v = np.array([[x, y, z] for x in (0, size) for y in (0, size) for z in (0, size)], dtype=float)
    # Quads are listed inward-wound; reversed below so the cube faces outward.
    quads = [(0, 2, 3, 1), (4, 5, 7, 6), (0, 1, 5, 4), (2, 6, 7, 3), (0, 4, 6, 2), (1, 3, 7, 5)]
    tris = [(a, b, c) for a, b, c, d in quads] + [(a, c, d) for a, b, c, d in quads]
    return Mesh(v[np.array(tris)][:, ::-1])


def sample(name) -> bytes:
    return (SAMPLES / f"{name}.stl").read_bytes()


def test_cube_geometry():
    m = cube()
    assert m.volume() == pytest.approx(1000.0)
    classes = classify_faces(m, [0, 0, 1])
    assert (classes == BED).sum() == 2
    assert (classes == OK).sum() == 10


def test_binary_roundtrip():
    m = cube()
    again = load_stl(to_binary_stl(m))
    assert np.allclose(again.triangles, m.triangles)


def test_binary_stl_with_solid_header_and_trailing_bytes():
    data = to_binary_stl(cube())
    data = b"solid exported".ljust(80, b" ") + data[80:] + b"\0\0"
    assert load_stl(data).face_count == 12


def test_ascii_stl():
    data = b"""solid t
facet normal 0 0 1
outer loop
vertex 0 0 0
vertex 1 0 0
vertex 0 1 0
endloop
endfacet
endsolid t"""
    assert load_stl(data).face_count == 1


def test_invalid_stl():
    with pytest.raises(ValueError):
        load_stl(b"not an stl at all" * 10)


def test_ceiling_needs_support():
    m = cube()
    # Lift the cube: the bottom faces are no longer on the bed but still point down.
    lifted = Mesh(np.vstack([m.triangles, m.triangles + [0, 0, 20]]))
    classes = classify_faces(lifted, [0, 0, 1])
    assert (classes == NEEDS_SUPPORT).sum() == 2


def test_rotation_to_z():
    for up in ([1, 0, 0], [0, 0, -1], [1, 2, 3], [0, 0, 1]):
        u = np.array(up, float) / np.linalg.norm(up)
        r = rotation_to_z(u)
        assert np.allclose(r @ u, [0, 0, 1])
        assert np.allclose(r @ r.T, np.eye(3))


@pytest.mark.skipif(not SAMPLES.exists(), reason="run tools/make_samples.py")
def test_mushroom_flipped():
    best = find_orientations(load_stl(sample("mushroom")))[0]
    assert np.allclose(best.up, [0, 0, -1], atol=1e-6)
    assert best.support_area == pytest.approx(0.0)


@pytest.mark.skipif(not SAMPLES.exists(), reason="run tools/make_samples.py")
def test_hook_on_its_side():
    best = find_orientations(load_stl(sample("hook")))[0]
    assert abs(best.up[0]) == pytest.approx(1.0)


@pytest.mark.skipif(not SAMPLES.exists(), reason="run tools/make_samples.py")
def test_api_orientations_payload():
    info = api.load(sample("l_bracket"), "l_bracket.stl")
    faces = info["faces"]
    assert len(info["triangles"]) == faces * 36
    r = api.orientations({"material": "petg"})
    for o in [r["as_loaded"], *r["orientations"]]:
        assert len(o["face_classes"]) == faces
        assert o["reasons"]


@pytest.mark.parametrize(
    "name, expected_up",
    [
        ("enclosure", [[0, 0, 1]]),  # open side up
        ("phone_stand", [[0, 1, 0], [0, -1, 0]]),  # lying on the big extruded side
        ("cable_clip", [[0, 0, 1], [0, 0, -1]]),  # flat on its side
        ("mushroom", [[0, 0, -1]]),
        ("hook", [[1, 0, 0], [-1, 0, 0]]),
    ],
)
@pytest.mark.skipif(not SAMPLES.exists(), reason="run tools/make_samples.py")
def test_typical_parts_orientation(name, expected_up):
    api.load(sample(name), name)
    options = api.orientations()["orientations"]
    assert any(np.allclose(options[0]["up"], e, atol=1e-6) for e in expected_up), options[0]["up"]
    assert all(o["bed_area_mm2"] > 0 for o in options), "options without bed contact should be hidden"
