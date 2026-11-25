import json
from pathlib import Path

import numpy as np
import pytest
from shapely.geometry import box

from fdmcore import api, strength
from fdmcore.additions import extrude
from fdmcore.fea import Material, build_model, element_stress, solve
from fdmcore.mesh import Mesh, load_stl
from fdmcore.profile import Profile
from fdmcore.regions import find_regions, region_info
from fdmcore.voxel import VoxelGrid, voxelize

SAMPLES = Path(__file__).resolve().parents[2] / "web" / "samples"


def test_voxel_volume_close_to_mesh():
    m = load_stl((SAMPLES / "mushroom.stl").read_bytes())
    g = voxelize(m, 20_000)
    # Thin sections gain/lose a partial voxel layer, so ~10% is expected at this resolution.
    assert g.solid.sum() * g.h**3 == pytest.approx(m.volume(), rel=0.12)


def test_partial_voxels_keep_volume_and_thin_walls():
    m = load_stl((SAMPLES / "mushroom.stl").read_bytes())
    g = voxelize(m, 20_000, partial=True)
    assert g.fill.sum() * g.h**3 == pytest.approx(m.volume(), rel=0.01)
    for t in (0.6, 1.0, 1.4, 2.2):  # plates thinner than a 2.5 mm cube: binary voxels make them all 2.5 mm
        plate = Mesh(extrude(box(0, 0, 40, 30), 0, t))
        assert voxelize(plate, h=2.5, partial=True).fill.sum() * 2.5**3 / 1200 == pytest.approx(t, rel=0.01)
        wall = Mesh(extrude(box(0, 0, t, 30), 0, 40))  # across the rays: resolved to a third of a cube
        assert voxelize(wall, h=2.5, partial=True).fill.sum() * 2.5**3 / 1200 == pytest.approx(t, abs=2.5 / 3)


def test_thin_strip_strength_follows_its_thickness():
    """Two strips thinner than one cube, pulled lengthwise: binary voxels can't tell them apart. With
    partial cubes the 1 mm strip is all top and bottom layers (0.4 of a cube), the 2 mm strip 70 %
    solid and 30 % infill (0.8 × (0.7 + 0.3 × 0.15^1.5) ≈ 0.57 of a cube): ~1.43× stiffer and stronger."""
    out = []
    for t in (1.0, 2.0):
        m = Mesh(extrude(box(0, 0, 60, 10), 0, t))
        n, _ = m.normals_and_areas()
        region = np.where(n[:, 0] < -0.9, 1, np.where(n[:, 0] > 0.9, 2, 0))
        loads = [{"region": 1, "kind": "fixed"}, {"region": 2, "kind": "force", "kg": 5, "direction": "pull"}]
        case = strength.prepare(m, region, loads, model=build_model(voxelize(m, h=2.5, partial=True)))
        out.append(strength.evaluate(case, [0, 0, 1], Profile.from_dict({"material": "pla"})))
    assert out[0]["max_displacement_mm"] / out[1]["max_displacement_mm"] == pytest.approx(1.43, rel=0.1)
    assert out[1]["safety_factor"] / out[0]["safety_factor"] == pytest.approx(1.43, rel=0.1)


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
    back = api.dispatch("strength", json.dumps({"up": [0, 1, 0], "loads": loads, "profile": prof}))
    assert side > 1.5 * back["safety_factor"]
    assert back["mode"] == "layers"
    low, high = back["range"]
    assert low < back["safety_factor"] <= high
    json.dumps(back, allow_nan=False, default=bytes.hex)


def test_safety_range_rechecks_every_load_and_keeps_material_failure_limit():
    profile = Profile.from_dict({"material": "pla"})
    assert strength.safety_range([(0.4, 0.1), (0.2, 0.375)], profile, 2.5) == pytest.approx([2.0, 1 / 0.375])


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


def test_several_load_cases_weakest_decides():
    fixed, force = hook_loads()
    prof = {"material": "pla"}
    light = api.strength([1, 0, 0], [fixed, {**force, "kg": 2}], prof)["safety_factor"]
    heavy = api.strength([1, 0, 0], [fixed, {**force, "kg": 8}], prof)["safety_factor"]
    both = api.strength([1, 0, 0], [fixed, {**force, "kg": 2, "case": 1}, {**force, "kg": 8, "case": 2}], prof)
    assert both["safety_factor"] == pytest.approx(heavy, rel=1e-3) and both["case"] == 2
    assert [c["safety_factor"] for c in both["cases"]] == pytest.approx([light, heavy], rel=1e-3)
    # Same forces in one case add up instead: weaker than either alone.
    assert api.strength([1, 0, 0], [fixed, {**force, "kg": 2}, {**force, "kg": 8}], prof)["safety_factor"] < heavy


def test_point_load_lever_matters():
    """The same 2 kg pressed on a spot at the arm's tip is much harder on the hook than near its root."""
    fixed, _ = hook_loads()
    prof = {"material": "pla"}
    tip = api.strength([1, 0, 0], [fixed, {"point": [0, 30, 22], "radius": 2, "kind": "force", "kg": 2, "direction": "down"}], prof)
    root = api.strength([1, 0, 0], [fixed, {"point": [0, 5, 10.5], "radius": 2, "kind": "force", "kg": 2, "direction": "down"}], prof)
    assert tip["safety_factor"] < 0.6 * root["safety_factor"]
    with pytest.raises(ValueError, match="No surface"):
        api.strength([1, 0, 0], [fixed, {"point": [0, 300, 0], "kind": "force", "kg": 2}], prof)


def test_loose_pieces_dont_break_the_solver():
    """A piece joined only by a link thinner than a voxel is cut off in the analysis grid. That used
    to make the solver fail ("exactly singular"); now the loose piece is held still, and loading
    only that piece is explained."""
    m3d = pytest.importorskip("manifold3d")
    from fdmcore.mesh import Mesh, to_binary_stl

    M = m3d.Manifold
    part = M.cube([60, 10, 10]) + M.cube([10, 10, 10]).translate([0, 30, 0]) + M.cube([0.3, 21, 0.3]).translate([5, 9.5, 5])
    m = part.to_mesh()
    api.load(to_binary_stl(Mesh(m.vert_properties[:, :3][m.tri_verts].astype(float))), "linked")
    info = api.regions()["regions"]
    bar = [i for i in info if i["centroid"][1] < 9 and i["area_mm2"] > 50]
    end = min(bar, key=lambda i: i["normal"][0])["id"]  # x = 0 face of the bar
    tip = max(bar, key=lambda i: i["normal"][0])["id"]  # x = 60 face
    loose = max((i for i in info if i["centroid"][1] > 25 and i["area_mm2"] > 50), key=lambda i: i["normal"][2])["id"]  # cube top
    r = api.strength([0, 0, 1], [{"region": end, "kind": "fixed"}, {"region": tip, "kind": "force", "kg": 2, "direction": "down"}])
    assert r["solver_converged"] and r["safety_factor"] > 1
    with pytest.raises(ValueError, match="isn't connected"):
        api.strength([0, 0, 1], [{"region": end, "kind": "fixed"}, {"region": loose, "kind": "force", "kg": 2}])


def _plank():
    """A 100 × 20 × 6 mm plank: held at one end, 1 kg hanging from the other."""
    from shapely.geometry import box

    from fdmcore.additions import extrude
    from fdmcore.mesh import Mesh, to_binary_stl

    api.load(to_binary_stl(Mesh(extrude(box(0, 0, 100, 20), 0, 6))), "plank")
    info = api.regions()["regions"]
    end = min(info, key=lambda i: i["normal"][0])["id"]
    other = max(info, key=lambda i: i["normal"][0])["id"]
    return [{"region": end, "kind": "fixed"}, {"region": other, "kind": "force", "kg": 1, "direction": "down"}]


def test_top_and_bottom_layers_carry_bending_when_printed_flat():
    loads = _plank()
    prof = {"material": "pla", "printer": "bambu_p1s"}
    base = dict(walls=2, infill=0.1, top=3, bottom=3)

    def run(up, **kw):
        return api.strength(up, loads, prof, **{**base, **kw})

    # Printed flat, the top and bottom skins are the outer fibres of the bent plank: thicker skins
    # stiffen it more than thicker walls along its sides (walls also wrap the clamped end, so
    # compare how far it bends, not the stress right at the clamp).
    flat = lambda **kw: run([0, 0, 1], **kw)["max_displacement_mm"]
    assert flat(top=8, bottom=8) < flat(walls=7) < flat()
    assert run([0, 0, 1], top=8, bottom=8)["safety_factor"] > 1.3 * run([0, 0, 1])["safety_factor"]
    # Standing on its long edge, the same skins sit on the narrow sides: walls take over.
    edge = lambda **kw: run([0, 1, 0], **kw)["max_displacement_mm"]
    assert edge(walls=7) < edge(top=8, bottom=8)


def test_optimiser_finds_the_lightest_settings_that_hold():
    loads = _plank()
    prof = {"material": "pla", "printer": "bambu_p1s"}
    o = api.optimise_strength([0, 0, 1], loads, prof)
    assert o["met"] and o["safety_factor"] >= 2.0 and o["target"] == 2.0
    assert o["settings"]["wall_loops"] == str(o["walls"])
    assert "reach" in o["explanation"] and "beyond" not in o
    check = api.strength(o["up"], loads, prof, o["walls"], o["infill"], o["top"], o["bottom"])
    assert check["safety_factor"] == pytest.approx(o["safety_factor"], rel=1e-6)
    heavy = [dict(l, kg=500) if l["kind"] == "force" else l for l in loads]
    o = api.optimise_strength([0, 0, 1], heavy, prof)
    assert not o["met"] and "can't make it strong enough" in o["explanation"]
    # Too weak with every setting: say what else would do it, e.g. a load it does hold.
    less = next(b for b in o["beyond"] if b["kind"] == "load")
    assert less["safety_factor"] == o["target"] and "kg instead of 500 kg" in less["change"]
    lighter = [dict(l, kg=500 * less["load_share"]) if l["kind"] == "force" else l for l in loads]
    held = api.strength(o["up"], lighter, prof, o["walls"], o["infill"], o["top"], o["bottom"])
    assert held["safety_factor"] == pytest.approx(o["target"], rel=1e-3)


def test_optimiser_for_this_orientation_never_turns_the_part():
    """The app's automatic settings for a load (T-429): same climb, this orientation only, no extras."""
    loads = _plank()
    prof = {"material": "pla", "printer": "bambu_p1s"}
    for up in ([0, 0, 1], [0, 1, 0]):
        o = api.optimise_strength(up, loads, prof, this_orientation=True)
        assert o["orientation"] == "this one" and o["alternatives"] == [] and o["up"] == up
    heavy = [dict(l, kg=500) if l["kind"] == "force" else l for l in loads]
    o = api.optimise_strength([0, 0, 1], heavy, prof, this_orientation=True)
    assert not o["met"] and "beyond" not in o



def test_optimiser_counts_print_time_and_compares_with_a_baseline():
    """Walls take longer per gram than infill, so the climb weighs time as well as plastic (T-429),
    and the goal's settings come back with their safety, grams and minutes for the comparison."""
    from fdmcore.checks import shell_infill_minutes
    from fdmcore.profile import Profile

    p = Profile.from_dict({"material": "pla", "printer": "bambu_p1s"})
    assert shell_infill_minutes(1000, 0, p) > shell_infill_minutes(0, 1000, p) > 0
    loads = [dict(l, kg=l["kg"] * 3) if l["kind"] == "force" else l for l in _plank()]
    base = {"walls": 3, "infill": 0.15, "top": 4, "bottom": 3}
    o = api.optimise_strength([0, 0, 1], loads, {"material": "pla", "printer": "bambu_p1s"}, this_orientation=True, baseline=base)
    assert o["met"] and o["minutes"] > 0 and o["grams"] > 0
    b = o["baseline"]
    check = api.strength([0, 0, 1], loads, {"material": "pla", "printer": "bambu_p1s"}, **base)
    assert b["safety_factor"] == pytest.approx(check["safety_factor"], rel=1e-6) and b["grams"] > 0 and b["minutes"] > 0


def test_fea_view_fields_cover_the_refined_surface():
    loads = hook_loads()
    r = api.strength([1, 0, 0], loads, {"material": "pla"})
    tris = np.frombuffer(r["field_triangles"], np.float32).reshape(-1, 3, 3)
    fi = np.frombuffer(r["vertex_fi"], np.float32)
    disp = np.frombuffer(r["vertex_disp"], np.float32).reshape(-1, 3)
    assert len(fi) == len(disp) == 3 * len(tris) > 46 * 3  # finer than the 46-triangle hook
    edges = np.linalg.norm(tris - np.roll(tris, 1, axis=1), axis=2).max()
    assert edges <= r["mesh"]["voxel_mm"] + 1e-3
    assert 0 <= fi.min() and fi.max() <= r["fi_max_pct"] * 3  # surface colours on the same scale as the verdict
    assert r["fi_max_pct"] == pytest.approx(100 / r["safety_factor"], rel=1e-6)
    assert np.abs(disp).max() <= r["max_displacement_mm"] * 1.01
    assert r["mesh"]["load_n"] == pytest.approx(5 * 9.81, rel=1e-3)
    centers = np.frombuffer(r["elements"]["centers"], np.float32)
    assert len(centers) == 3 * r["mesh"]["voxels"]


def test_a_held_face_is_held_everywhere_not_at_a_few_points():
    """CAD faces are often two big triangles: every voxel under the face must be held / loaded."""
    api.load((SAMPLES / "mushroom.stl").read_bytes(), "m")
    api.regions()
    case = api._load_case([{"region": 8, "kind": "fixed"}, {"region": 0, "kind": "force", "kg": 20, "direction": "pull"}])[0]
    nodes = np.argwhere(case.model.node_index >= 0)
    top = nodes[:, 2] == nodes[:, 2].max()
    bottom = nodes[:, 2] == nodes[:, 2].min()
    assert case.fixed[top].all()
    assert (np.abs(case.forces[bottom]).sum(axis=1) > 0).all()


def test_stronger_orientation_offered_when_it_holds_more():
    """T-368: the mushroom standing on its cap, pulled on the stem, pulls its layers apart; lying on its side
    holds about twice as much and is offered. Already lying that way, nothing is."""
    from pathlib import Path

    api.load((Path(__file__).resolve().parents[2] / "web" / "samples" / "mushroom.stl").read_bytes(), "mushroom")
    api.regions()
    loads = [{"region": 8, "kind": "fixed"}, {"region": 0, "kind": "force", "kg": 30, "direction": "pull"}]
    o = api.stronger_orientation([0, 0, 1], loads, {"material": "pla"})
    assert o["up"] is not None and abs(o["up"][2]) < 0.01 and o["safety_factor"] > 1.5 * o["current"]
    assert api.stronger_orientation(o["up"], loads, {"material": "pla"})["up"] is None


def test_painted_area_holds_like_the_face_it_covers():
    """A brush stroke (T-370): spots painted over the whole end of a bar hold it about as well as that
    end face, and much better than one small spot in its middle."""
    from fdmcore.mesh import to_binary_stl

    api.load(to_binary_stl(Mesh(extrude(box(0, 0, 40, 10), 0, 6))), "bar")
    end = next(r["id"] for r in api.regions()["regions"] if r["normal"][0] < -0.9)
    pull = {"point": [40, 5, 3], "radius": 3, "kind": "force", "kg": 5, "direction": "down"}
    painted = [[0, y, z] for y in (1, 3, 5, 7, 9) for z in (1, 3, 5)]

    def safety(held):
        return api.strength([0, 0, 1], [held, pull])["safety_factor"]

    face = safety({"region": end, "kind": "fixed"})
    brush = safety({"points": painted, "radius": 1.5, "kind": "fixed"})
    spot = safety({"point": [0, 5, 3], "radius": 1.5, "kind": "fixed"})
    assert brush == pytest.approx(face, rel=0.25)
    assert brush > 1.3 * spot
    with pytest.raises(ValueError, match="painted area"):
        api.strength([0, 0, 1], [{"points": [[0, 50, 50], [0, 52, 50]], "radius": 1, "kind": "fixed"}, pull])
