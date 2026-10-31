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
