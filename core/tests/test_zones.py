import io
import zipfile

import numpy as np
import pytest

from fdmcore import api
from fdmcore.strength import Shells, Zone, element_scale, find_zones
from fdmcore.profile import Profile
from fdmcore.recommend import goal_plan

from test_hold import box, region_facing


def cantilever(kg=4):
    api.load(box(120, 12, 12), "bar")
    return [{"region": region_facing([-1, 0, 0]), "kind": "fixed"}, {"region": region_facing([1, 0, 0]), "kind": "force", "kg": kg, "direction": "down"}]


def test_zone_changes_only_the_cubes_inside():
    loads = cantilever()
    model = api._load_case(loads)[0].model
    p = Profile.from_dict({})
    base = Shells(walls=2, infill=0.1)
    zone = Zone((0.0, 0.0, 0.0), (30.0, 12.0, 12.0), walls=2, infill=1.0)
    plain, zoned = element_scale(model, [0, 0, 1], base, p), element_scale(model, [0, 0, 1], Shells(walls=2, infill=0.1, zones=(zone,)), p)
    inside = model.grid.centers(model.elems)[:, 0] <= 30
    assert np.all(zoned[inside] >= plain[inside]) and zoned[inside].mean() > plain[inside].mean()
    assert np.allclose(zoned[~inside], plain[~inside])


def test_zones_sit_where_the_stress_is():
    loads = cantilever()
    cases = api._load_case(loads)
    r = api.strength([0, 0, 1], loads, walls=2, infill=0.1)
    from fdmcore.strength import evaluate_cases

    e = evaluate_cases(cases, [0, 0, 1], Profile.from_dict({}), Shells(walls=2, infill=0.1))
    zones = find_zones(cases[0].model, e["fi"], cases[0].ignore, [0, 0, 1], 2, 0.1)
    assert zones and all(z.lo[0] < 20 for z in zones)  # next to the clamp at x = 0
    assert all(z.hi[0] < 100 for z in zones)  # not the whole bar
    assert r["critical_point"][0] < 30


def test_zoned_optimiser_is_not_heavier():
    r = api.optimise_strength([0, 0, 1], cantilever(), {"material": "pla"}, zones=True)
    z = r["zoned"]
    assert z["met"] and z["zones"] and z["grams"] <= r["grams"] + 0.1
    assert "zone" in z["explanation"]
    assert len(z["zones"][0]["corners_print"]) == 8
    # Time follows the plastic saved: none saved, no time; more saved, more time.
    assert z["saves_min"] >= 0 and (z["saves_min"] > 0) == (z["saves_grams"] > 0)
    p = Profile.from_dict({"material": "pla"})
    assert api._grams_minutes(20, p) == pytest.approx(2 * api._grams_minutes(10, p))
    assert 1 < api._grams_minutes(10, p) < 30  # 10 g of PLA infill: minutes, not seconds or hours


def test_picked_zone_wraps_the_face_and_counts():
    """A zone the user picks around the clamped end: a box just past that face, stronger there."""
    loads = cantilever()
    z = api.zone_at([0, 0, 1], region=loads[0]["region"], margin=3, infill=1.0)
    assert z["lo"][0] == pytest.approx(-3, abs=0.01) and z["hi"][0] == pytest.approx(3, abs=0.01)
    assert z["lo"][2] == pytest.approx(-3, abs=0.01) and z["hi"][2] == pytest.approx(15, abs=0.01)
    assert z["walls"] == goal_plan(Profile.from_dict({}))["walls"] + 2 and z["infill"] == 1.0
    spot = api.zone_at([0, 0, 1], point=[60, 6, 12], margin=5)
    assert spot["hi"][0] - spot["lo"][0] == pytest.approx(10) and len(spot["corners_print"]) == 8
    # Reaching well past the clamp it carries the bending there (a thin slice would only move the weak spot to its edge).
    wide = api.zone_at([0, 0, 1], region=loads[0]["region"], margin=30, infill=1.0)
    plain = api.strength([0, 0, 1], loads, walls=2, infill=0.1)["safety_factor"]
    zoned = api.strength([0, 0, 1], loads, walls=2, infill=0.1, zones=[wide])["safety_factor"]
    assert zoned > 1.1 * plain
    with pytest.raises(ValueError):
        api.zone_at([0, 0, 1])


def test_export_layouts():
    api.load(box(40, 20, 10), "block")
    zone = {"lo": [-1, -1, 0], "hi": [20, 21, 10], "walls": 3, "infill": 0.5}
    orca = zipfile.ZipFile(io.BytesIO(api.export([0, 0, 1], {}, {"wall_loops": "2"}, zones=[zone], slicer="orca")))
    conf = orca.read("Metadata/model_settings.config").decode()
    assert 'subtype="modifier_part"' in conf and 'key="sparse_infill_density" value="50%"' in conf
    assert "<components>" in orca.read("3D/3dmodel.model").decode()
    prusa = zipfile.ZipFile(io.BytesIO(api.export([0, 0, 1], {}, {"wall_loops": "2"}, zones=[zone], slicer="prusa")))
    conf = prusa.read("Metadata/Slic3r_PE_model.config").decode()
    assert "ParameterModifier" in conf and 'key="fill_density" value="50%"' in conf and 'key="perimeters" value="3"' in conf
    plain = zipfile.ZipFile(io.BytesIO(api.export([0, 0, 1], {}, {"wall_loops": "2"})))
    assert {"Metadata/model_settings.config", "Metadata/Slic3r_PE_model.config"} <= set(plain.namelist())
    with pytest.raises(ValueError, match="layout"):
        api.export([0, 0, 1], {}, {}, zones=[zone], slicer="cura")


def test_zone_box_stays_above_the_bed():
    api.load(box(40, 20, 10), "block")
    tris = api._zone_box(api._require().mesh, [0, 0, 1], np.eye(3), {"lo": [0, 0, -5], "hi": [10, 10, 50]})
    assert tris[..., 2].min() == pytest.approx(0) and tris[..., 2].max() == pytest.approx(10)


def test_round_zone_shapes():
    q = np.array([[5.0, 5, 5], [9.9, 5, 5], [9.5, 9.5, 5], [5, 5, 10.5]])
    box_ = Zone((0.0, 0, 0), (10.0, 10, 10), 3, 0.5)
    cyl = Zone((0.0, 0, 0), (10.0, 10, 10), 3, 0.5, shape="cylinder")
    ball = Zone((0.0, 0, 0), (10.0, 10, 10), 3, 0.5, shape="ball")
    assert np.allclose(box_.gap(q)[:3], 0) and cyl.gap(q)[2] == pytest.approx(np.hypot(4.5, 4.5) - 5, abs=1e-6)
    assert cyl.gap(q)[1] == 0 and ball.gap(q)[3] == pytest.approx(0.5) and cyl.gap(q)[3] == pytest.approx(0.5)
    for shape, share in api.SHAPE_VOLUME.items():  # closed, outward meshes filling the right share of the cube
        t = api._unit_shape(shape)
        vol = np.einsum("ij,ij->i", t[:, 0], np.cross(t[:, 1], t[:, 2])).sum() / 6
        assert vol == pytest.approx(8 * share, rel=0.03)


def test_zone_around_an_upright_hole_is_a_cylinder():
    from shapely.geometry import Point, box as rect

    from fdmcore.additions import extrude
    from fdmcore.mesh import Mesh, to_binary_stl

    api.load(to_binary_stl(Mesh(extrude(rect(-20, -20, 20, 20).difference(Point(0, 0).buffer(3, 32)), 0, 8))), "plate")
    hole = next(f for f in api.design([0, 0, 1])["features"] if f["type"] == "hole")
    z = api.zone_at([0, 0, 1], region=hole["region"], margin=3)
    assert z["shape"] == "cylinder" and z["hi"][0] - z["lo"][0] == pytest.approx(12, abs=0.3)
    assert api.zone_at([0, 0, 1], region=hole["region"], shape="box")["shape"] == "box"
    conf = zipfile.ZipFile(io.BytesIO(api.export([0, 0, 1], {}, {"wall_loops": "2"}, zones=[z]))).read("3D/3dmodel.model").decode()
    assert conf.count("<triangle ") > 12 + 100  # the part plus a round modifier, not a 12-triangle box
    with pytest.raises(ValueError, match="shape"):
        api.zone_at([0, 0, 1], region=hole["region"], shape="cone")


def test_strength_proposes_zones_where_the_stress_is():
    """A cantilever is stressed most at its fixed end: the strength check proposes a zone there,
    the most stressed first, and using it makes the part stronger."""
    loads = cantilever()
    r = api.strength([0, 0, 1], loads, {})
    zones = r["stress_zones"]
    assert zones and zones[0]["share_pct"] == 100
    lo, hi = np.asarray(zones[0]["lo"]), np.asarray(zones[0]["hi"])
    root = api._require().mesh.triangles[..., 0].min()
    assert lo[0] - root < 5 and hi[0] - lo[0] < 80  # at the fixed end, not the whole bar
    assert zones[0]["walls"] == r["walls"] + 2 and zones[0]["infill"] >= max(0.4, r["infill"])
    stronger = api.strength([0, 0, 1], loads, {}, zones=zones)
    assert stronger["safety_factor"] > r["safety_factor"]


def test_lightest_settings_put_the_zones_where_your_strength_check_found_the_stress():
    """T-290: after a strength check for the same orientation, loads and material, "Find the lightest settings" places
    its zones from that result (the same places "Stronger in one area" offers); otherwise from a light print."""
    loads = cantilever()
    assert api.optimise_strength([0, 0, 1], loads, {"material": "pla"}, zones=True)["zoned"]["zones_from"] == "light print"
    r = api.strength([0, 0, 1], loads, {"material": "pla"})
    z = api.optimise_strength([0, 0, 1], loads, {"material": "pla"}, zones=True)["zoned"]
    assert z["zones_from"] == "strength result" and "where your strength check found the stress" in z["explanation"]
    boxes = lambda zs: [(tuple(np.round(q["lo"], 2)), tuple(np.round(q["hi"], 2))) for q in zs]  # noqa: E731
    assert boxes(z["zones"]) == boxes(r["stress_zones"])
    # Another material (or orientation, or loads) than the check: its stress doesn't apply.
    assert api.optimise_strength([0, 0, 1], loads, {"material": "petg"}, zones=True)["zoned"]["zones_from"] == "light print"
