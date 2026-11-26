"""Textbook benchmarks and integration for the optional strength solves."""

import numpy as np
import pytest
from shapely.geometry import box

from fdmcore import api, strength
from fdmcore.additions import extrude
from fdmcore.fea import Material, buckling_load_factor, build_model, element_stress, large_deflection, solve
from fdmcore.mesh import Mesh, to_binary_stl
from fdmcore.profile import Profile
from fdmcore.voxel import VoxelGrid


def _cantilever(shape, force):
    model = build_model(VoxelGrid(np.ones(shape, bool), np.zeros(3), 1.0))
    fixed = np.zeros(model.n_nodes, bool)
    fixed[model.node_index[0].ravel()] = True
    tip = model.node_index[-1].ravel()
    forces = np.zeros((model.n_nodes, 3))
    forces[tip] = np.array(force) / len(tip)
    return strength.LoadCase(model, forces, fixed, np.zeros(len(model.elems), bool)), tip


def test_fixed_free_column_matches_euler():
    # E=1000 MPa, L=40 mm, I=4*4³/12 mm⁴, effective length 2L.
    # The test applies 20 N and compares the recovered critical load within 4%.
    case, _ = _cantilever((40, 4, 4), [-20, 0, 0])
    c = Material(1000, 1000, 0.3, 0.3).stiffness_print_frame()
    scale = np.ones(len(case.model.elems))
    u, info = solve(case.model, c, scale, case.forces, case.fixed, rtol=1e-7)
    assert info == 0
    result = buckling_load_factor(case.model, c, scale, case.fixed, element_stress(case.model, c, scale, u))
    assert result is not None
    factor, mode = result
    assert 20 * factor == pytest.approx(32.89868133696, rel=0.04)
    assert np.all(mode[case.fixed] == 0)
    assert np.linalg.norm(mode[:, 0]) < 0.1 * np.linalg.norm(mode)


def test_large_deflection_cantilever_matches_elastica():
    # P L²/EI = 1.5 gives 20 mm by linear beam theory. For the elastica, solve
    # theta'' = -1.5 cos(theta) on the unit interval, theta(0)=theta'(1)=0, and
    # integrate sin(theta). Bisshopp and Drucker (1945) give the equivalent elliptic integral.
    case, tip = _cantilever((40, 4, 4), [0, 0, -20])
    c = Material(1000, 1000, 0.3, 0.3).stiffness_print_frame()
    scale = np.ones(len(case.model.elems))
    linear, info = solve(case.model, c, scale, case.forces, case.fixed, rtol=1e-7)
    assert info == 0
    result = large_deflection(case.model, c, scale, case.forces, case.fixed, linear)
    assert result is not None
    curved, stress = result
    assert -linear[tip, 2].mean() == pytest.approx(20.0, rel=0.04)
    assert -curved[tip, 2].mean() == pytest.approx(16.4382350008, rel=0.04)
    assert curved[tip, 2].mean() / linear[tip, 2].mean() < 0.85
    assert curved[tip, 0].mean() < 0
    assert np.isfinite(stress).all()


def test_strength_accounts_for_large_tpu_motion():
    p = Profile.from_dict({"material": "tpu_95a"})
    force = strength.material_for(p).e_xy * (3 * 3**3 / 12) * 1.25 / 32**2
    case, _ = _cantilever((32, 3, 3), [0, 0, -force])
    result = strength.evaluate(case, [0, 0, 1], p, strength.Shells(walls=10))
    bend = result["geometry_checks"]["bending"]
    assert bend["status"] == "solved"
    assert bend["rule"] == "large_deflection"
    assert bend["displacement_mm"] == result["max_displacement_mm"]
    assert bend["displacement_mm"] < 0.96 * bend["linear_displacement_mm"]


def test_strength_explains_a_large_deflection_fallback(monkeypatch):
    p = Profile.from_dict({"material": "tpu_95a"})
    force = strength.material_for(p).e_xy * (3 * 3**3 / 12) * 1.25 / 32**2
    case, _ = _cantilever((32, 3, 3), [0, 0, -force])
    shells = strength.Shells(walls=10)
    reference = strength.evaluate(case, [0, 0, 1], p, shells, large_rotations=False)
    monkeypatch.setattr(strength, "large_deflection", lambda *_args: None)
    result = strength.evaluate(case, [0, 0, 1], p, shells)
    assert result["geometry_checks"]["bending"]["message"]["key"] == "strength.geometry.bending_unavailable"
    assert result["max_displacement_mm"] == reference["max_displacement_mm"]
    assert result["safety_factor"] == reference["safety_factor"]


def test_stiff_chunk_skips_both_optional_solves(monkeypatch):
    case, _ = _cantilever((10, 8, 8), [-10, 0, -10])

    def unexpected(*_args):
        pytest.fail("A stiff, chunky part should only use the linear strength solve")

    monkeypatch.setattr(strength, "large_deflection", unexpected)
    monkeypatch.setattr(strength, "buckling_load_factor", unexpected)
    result = strength.evaluate(case, [0, 0, 1], Profile.from_dict({"material": "pla"}))
    assert "geometry_checks" not in result
    assert result["solver_converged"]


def test_compressed_wall_buckling_sets_case_and_verdict(monkeypatch):
    mesh = Mesh(extrude(box(0, 0, 40, 12), 0, 3))
    api.load(to_binary_stl(mesh), "pressed wall")
    monkeypatch.setattr(api, "_analysis", {"strength_voxels": 1500, "fine_voxels": 2400})
    regions = api.regions()["regions"]
    root = min(regions, key=lambda r: r["normal"][0])["id"]
    tip = max(regions, key=lambda r: r["normal"][0])["id"]
    loads = [{"region": root, "kind": "fixed"},
             {"region": tip, "kind": "force", "direction": "pull", "case": 1, "kg": 20},
             {"region": tip, "kind": "force", "direction": "push", "case": 2, "kg": 20}]
    result = api.strength([0, 0, 1], loads, {"material": "pla"}, walls=10)
    assert result["case"] == 2
    assert result["mode"] == "buckling"
    assert result["verdict"] == "likely to break"
    buckling = result["geometry_checks"]["buckling"]
    assert "geometry_checks" not in result["cases"][0]
    assert buckling["load_factor"] == result["safety_factor"]
    assert buckling["load_factor"] == pytest.approx(0.734116, rel=1e-4)
    assert result["range"][1] == pytest.approx(buckling["load_factor"])
    assert buckling["message"]["key"] == "strength.geometry.buckling"
    assert any(fix["domain"] == "process" for fix in buckling["fixes"])
    assert "bow sideways" in result["explanation"]
