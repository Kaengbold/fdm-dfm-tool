from pathlib import Path

import numpy as np

from fdmcore import api
from fdmcore.mesh import Mesh, load_stl, mesh_health, to_binary_stl


def cube():
    v = np.array([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0),
                  (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)], float)
    f = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
         (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    return v[np.array(f)]


def test_cube_and_hole():
    h = mesh_health(Mesh(cube()))
    assert (h["holes"], h["flipped_faces"], h["shells"]) == (0, 0, 1)
    h = mesh_health(Mesh(cube()[2:]))
    assert (h["holes"], h["boundary_edges"]) == (1, 4)
    assert len(h["points"]["holes"]) == 1
    assert api.load(to_binary_stl(Mesh(cube()[2:])), "holed.stl")["mesh_health"]["holes"] == 1


def test_flipped_and_zero_area():
    t = cube()
    t[0] = t[0, ::-1]
    assert mesh_health(Mesh(t))["flipped_faces"] == 1
    t[0, 1] = t[0, 0]
    assert mesh_health(Mesh(t))["zero_area_faces"] == 1


def test_non_manifold_and_separate_shells():
    t = cube()
    extra = np.array([t[0, 0], t[0, 1], [0.5, 0.5, -1]])
    h = mesh_health(Mesh(np.concatenate((t, extra[None]))))
    assert h["non_manifold_edges"] == 1
    assert len(h["points"]["non_manifold_edges"]) == 1
    assert mesh_health(Mesh(np.concatenate((t, t + 3))))["shells"] == 2


def test_samples():
    for path in (Path(__file__).resolve().parents[2] / "web" / "samples").glob("*.stl"):
        h = mesh_health(load_stl(path.read_bytes()))
        assert (h["holes"], h["flipped_faces"]) == (0, 0), path.name
