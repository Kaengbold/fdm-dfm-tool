import numpy as np
import pytest

from fdmcore import api
from fdmcore.fea import Material, build_model, lowest_mode
from fdmcore.strength import impact_factor
from fdmcore.voxel import VoxelGrid

from test_hold import box, region_facing


def test_cantilever_frequency_matches_beam_theory():
    # L=100, 10x10 section, E=1000 MPa, density 1.2 g/cm³: f1 = 1.875² / (2π) · sqrt(EI / (ρ A L⁴))
    grid = VoxelGrid(np.ones((50, 5, 5), bool), np.zeros(3), 2.0)
    model = build_model(grid)
    c = Material(1000, 1000, 0.3, 0.3).stiffness_print_frame()
    fixed = np.zeros(model.n_nodes, bool)
    root = model.node_index[0].ravel()
    fixed[root[root >= 0]] = True
    kg = 1.2e-6 * 8.0  # per element: 2 mm cubes
    nodes = (model.edof[:, 0::3] // 3).ravel()
    mass = np.bincount(nodes, np.full(len(nodes), kg / 8), minlength=model.n_nodes)
    hz, shape = lowest_mode(model, c, np.ones(len(model.elems)), mass, fixed)
    e, i, rho, a, l = 1000e6, 0.01**4 / 12, 1200, 0.01**2, 0.1
    expected = 1.875**2 / (2 * np.pi) * np.sqrt(e * i / (rho * a * l**4))
    assert hz == pytest.approx(expected, rel=0.1)
    swing = lambda plane: np.linalg.norm(shape[plane.ravel()[plane.ravel() >= 0]], axis=1).mean()
    assert swing(model.node_index[-1]) > 5 * swing(model.node_index[12])  # the free end swings most (beam: 10x a quarter in)


def test_impact_factor():
    assert impact_factor(0, 1.0) == pytest.approx(2.0)  # set down suddenly
    assert impact_factor(100, 0.5) == pytest.approx(1 + np.sqrt(401))


def test_dynamics_api():
    api.load(box(120, 12, 12), "bar")
    loads = [{"region": region_facing([-1, 0, 0]), "kind": "fixed"}, {"region": region_facing([1, 0, 0]), "kind": "force", "kg": 1, "direction": "down"}]
    calm = api.dynamics([0, 0, 1], loads, {"material": "pla"})
    heavy = api.dynamics([0, 0, 1], loads, {"material": "pla"}, mounted_kg=0.5)
    dropped = api.dynamics([0, 0, 1], loads, {"material": "pla"}, drop_mm=50)
    assert 20 < calm["frequency_hz"] < 2000 and heavy["frequency_hz"] < calm["frequency_hz"]
    assert dropped["impact_factor"] > 5 and dropped["impact_safety"] < dropped["static_safety"] / 5
    assert "Hz" in calm["explanation"] and "drops 50 mm" in dropped["explanation"]


def test_dynamics_runs_only_what_is_asked_and_prints_like_strength():
    """Knocks alone skip the swing solve, shaking alone the static one; the knock check uses the
    same walls / infill as "Will it hold?" and its solve (cached, so it costs nothing the second time)."""
    api.load(box(120, 12, 12), "bar")
    loads = [{"region": region_facing([-1, 0, 0]), "kind": "fixed"}, {"region": region_facing([1, 0, 0]), "kind": "force", "kg": 1, "direction": "down"}]
    knock = api.dynamics([0, 0, 1], loads, {}, drop_mm=20, shake=False, infill=1.0)
    assert knock["frequency_hz"] is None and "vertex_disp" not in knock and "Hz" not in knock["explanation"]
    assert knock["static_safety"] == pytest.approx(api.strength([0, 0, 1], loads, {}, infill=1.0)["safety_factor"])
    assert knock["static_safety"] > api.dynamics([0, 0, 1], loads, {}, drop_mm=20, shake=False)["static_safety"]
    swing = api.dynamics([0, 0, 1], loads, {})
    assert swing["static_safety"] is None and swing["frequency_hz"] > 0 and "drops" not in swing["explanation"]


def test_drop_counts_the_give_of_mount_and_falling_thing():
    """T-410, the audit's L-bracket: 5 kg dropped 50 mm onto it. The part alone bends 0.04 mm, which gave x51 and
    "it will likely break"; the mount and the falling thing give too (springs in series, thresholds.yaml "drop"),
    so the answer is a range from a hard object on a part screwed into wood to something soft."""
    from pathlib import Path

    from fdmcore.profile import load_data

    api.load((Path(__file__).parents[2] / "web" / "samples" / "l_bracket.stl").read_bytes(), "l_bracket")
    up = api.orientations()["orientations"][0]["up"]
    loads = [{"region": region_facing([0, 1, 0]), "kind": "fixed"}, {"region": region_facing([0, 0, 1]), "kind": "force", "kg": 5, "direction": "down"}]
    r = api.dynamics(up, loads, {"material": "pla"}, drop_mm=50, shake=False)
    D, weight = load_data("thresholds")["drop"], 5 * 9.81
    assert 0.03 < r["sag_mm"] < 0.05
    assert r["impact_factor"] == pytest.approx(impact_factor(50, r["sag_mm"] + weight / D["hard_n_per_mm"]), rel=0.02)
    assert r["impact_factor_soft"] == pytest.approx(impact_factor(50, r["sag_mm"] + weight / D["soft_n_per_mm"]), rel=0.02)
    assert 7 < r["impact_factor_soft"] < 10 < 30 < r["impact_factor"] < impact_factor(50, r["sag_mm"]) - 10  # well under the rigid x51
    assert r["impact_safety"] < 1 < 2 < r["impact_safety_soft"]
    text = r["explanation"]
    assert "screwed into wood" in text and "rubber-footed" in text and "between ×" in text  # names what it assumes
    assert "a hard one may break it" in text
