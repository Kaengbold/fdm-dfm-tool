from pathlib import Path

import numpy as np
import pytest

from fdmcore import api
from fdmcore.fea import Material, build_model, element_stress, solve
from fdmcore.mesh import load_stl
from fdmcore.regions import find_regions, region_info
from fdmcore.voxel import VoxelGrid, voxelize

SAMPLES = Path(__file__).resolve().parents[2] / "web" / "samples"


def test_voxel_volume_close_to_mesh():
    m = load_stl((SAMPLES / "mushroom.stl").read_bytes())
    g = voxelize(m, 20_000)
    # Thin sections gain/lose a partial voxel layer, so ~10% is expected at this resolution.
    assert g.solid.sum() * g.h**3 == pytest.approx(m.volume(), rel=0.12)


def test_cantilever_matches_beam_theory():
    # L=100, 10x10 section, E=1000 MPa, 10 N tip load: deflection = FL³/3EI = 4 mm.
    grid = VoxelGrid(np.ones((50, 5, 5), bool), np.zeros(3), 2.0)
    model = build_model(grid)
    c = Material(1000, 1000, 0.3, 0.3).stiffness_print_frame()
    fixed = np.zeros(model.n_nodes, bool)
    root = model.node_index[0].ravel()
    fixed[root[root >= 0]] = True
    tip = model.node_index[-1].ravel()
    tip = tip[tip >= 0]
    f = np.zeros((model.n_nodes, 3))
    f[tip, 2] = -10 / len(tip)
    u, info = solve(model, c, np.ones(len(model.elems)), f, fixed)
    assert info == 0
    assert -u[tip, 2].mean() == pytest.approx(4.0, rel=0.05)
    stress = element_stress(model, c, np.ones(len(model.elems)), u)
    assert np.isfinite(stress).all()


def test_regions_group_holes_and_planes():
    m = load_stl((SAMPLES / "l_bracket.stl").read_bytes())
    info = region_info(m, find_regions(m))
    curved = sorted(r["area_mm2"] for r in info if r["kind"] == "curved")
    assert curved == pytest.approx([2 * np.pi * 3 * 5, 2 * np.pi * 4 * 5], rel=0.02)


def hook_loads():
    api.load((SAMPLES / "hook.stl").read_bytes(), "hook")
    info = api.regions()["regions"]
    back = min(info, key=lambda i: i["normal"][1])["id"]
    top = max([i for i in info if i["normal"][2] > 0.99], key=lambda i: i["area_mm2"])["id"]
    return [{"region": back, "kind": "fixed"}, {"region": top, "kind": "force", "kg": 5, "direction": "down"}]


def test_hook_strongest_on_its_side():
    loads = hook_loads()
    prof = {"material": "abs", "printer": "prusa_mk4s"}
    side = api.strength([1, 0, 0], loads, prof)["safety_factor"]
    back = api.strength([0, 1, 0], loads, prof)
    assert side > 1.5 * back["safety_factor"]
    assert back["mode"] == "layers"


def test_enclosure_makes_abs_stronger_across_layers():
    loads = hook_loads()
    open_frame = api.strength([0, 1, 0], loads, {"material": "abs", "printer": "prusa_mk4s"})["safety_factor"]
    heated = api.strength([0, 1, 0], loads, {"material": "abs", "printer": "prusa_core_one"})["safety_factor"]
    assert heated > open_frame


def test_strength_ranks_orientations():
    loads = hook_loads()
    r = api.orientations({"material": "abs", "printer": "prusa_mk4s"}, loads=loads)
    best = r["orientations"][0]
    assert abs(best["up"][0]) == pytest.approx(1.0)
    assert best["safety_factor"] is not None


def test_strength_needs_fixed_and_load():
    api.load((SAMPLES / "hook.stl").read_bytes(), "hook")
    with pytest.raises(ValueError):
        api.strength([0, 0, 1], [{"region": 0, "kind": "force", "kg": 1}])
