import json

import numpy as np
import pytest

from fdmcore import api, cli, mcp_server, topopt
from fdmcore.mesh import Mesh, to_binary_stl
from fdmcore.profile import Profile
from fdmcore.strength import prepare_cases

BOX = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], float)
FACES = [[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]]


def beam():
    """A 120 × 10 × 30 mm cantilever: held at x = 0, 10 kg pulling down at the top of the free end."""
    api.load(to_binary_stl(Mesh((BOX * [120, 10, 30])[np.array(FACES)])), "beam.stl")
    regs = api.regions()["regions"]
    held = min(regs, key=lambda r: r["centroid"][0])["id"]
    end = max(regs, key=lambda r: r["centroid"][0])["id"]
    return [{"region": held, "kind": "fixed"}, {"region": end, "point": [120, 5, 30], "kind": "force", "kg": 10, "direction": "down"}]


def test_cantilever_becomes_a_truss():
    loads = beam()
    s = api._require()
    cases = prepare_cases(s.mesh, s.regions, loads, 1500)
    model = cases[0].model
    r = topopt.optimise(cases, [0, 1, 0], Profile.from_dict({}), 0.4, iterations=30)
    rho = r["density"]
    assert np.all(rho[r["passive"]] == 1.0)  # loads and the held end keep their plastic
    fill = np.broadcast_to(1.0 if model.grid.fill is None else model.grid.fill[tuple(model.elems.T)], rho.shape)
    assert (rho * fill).sum() / fill.sum() == pytest.approx(0.4, abs=0.01)  # the volume asked for
    assert r["compliance"][-1] < r["compliance"][0]  # stiffer for its weight than the even start
    # Near the wall the beam bends most: plastic goes to the top and bottom edges, not the middle.
    k = model.elems[:, 2]
    near = model.elems[:, 0] < model.grid.solid.shape[0] // 3
    edge = rho[near & ((k == 0) | (k == k.max()))].mean()
    middle = rho[near & (np.abs(k - k.max() / 2) <= 1)].mean()
    assert edge > middle + 0.3, (edge, middle)


def test_voxel_surface_is_closed():
    loads = beam()
    s = api._require()
    model = prepare_cases(s.mesh, s.regions, loads, 800)[0].model
    tris = topopt.voxel_surface(model.grid, model.elems[::2])
    edges = np.sort(np.round(np.stack([tris, np.roll(tris, -1, axis=1)], axis=2), 6).reshape(-1, 6).reshape(-1, 2, 3), axis=1)
    _, counts = np.unique(edges.reshape(-1, 6), axis=0, return_counts=True)
    assert np.all(counts % 2 == 0)  # every edge shared: no holes
    assert Mesh(tris).volume() == pytest.approx(len(model.elems[::2]) * model.grid.h**3, rel=1e-6)


def test_api_mcp_cli(tmp_path, capsys):
    loads = beam()
    r = api.topology([0, 1, 0], loads, keep=0.4)
    assert r["shape_g"] < 0.7 * r["part_g"] and 0 < r["safety_solid_shape"] <= r["safety_solid_part"]
    assert "CAD" in r["explanation"] and len(r["stl"]) > 84
    with pytest.raises(ValueError, match="keep"):
        api.topology([0, 1, 0], loads, keep=0.05)
    out = mcp_server.suggest_lighter_shape([0, 1, 0], loads, str(tmp_path / "s.stl"), keep=0.5)
    assert (tmp_path / "s.stl").stat().st_size > 84 and "stl" not in out
    stl = tmp_path / "beam.stl"
    stl.write_bytes(to_binary_stl(Mesh((BOX * [120, 10, 30])[np.array(FACES)])))
    assert cli.main(["topology", str(stl), "--up", "0,1,0", "--loads", json.dumps(loads), "-o", str(tmp_path / "c.stl")]) == 0
    assert json.loads(capsys.readouterr().out)["saved"] == str(tmp_path / "c.stl")
