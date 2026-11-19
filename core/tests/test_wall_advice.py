"""Wall thickness and line width advice (non-whole line multiples, load-bearing minimum, uneven thickness)."""

from pathlib import Path

import numpy as np
import pytest
import shapely
from shapely.geometry import box
from shapely.geometry.polygon import orient as orient_poly

from fdmcore import api
from fdmcore.checks import orient, run_checks
from fdmcore.mesh import Mesh
from fdmcore.profile import Profile, load_data

SAMPLES = Path(__file__).resolve().parents[2] / "web" / "samples"


def extrude_poly(poly, height: float) -> Mesh:
    """Closed vertical prism for any 2D polygon (with or without holes) or MultiPolygon."""
    tris = []
    for p in shapely.get_parts(poly):
        p = orient_poly(p.simplify(0), 1.0)
        caps = shapely.get_coordinates(shapely.get_parts(shapely.constrained_delaunay_triangles(p))).reshape(-1, 4, 2)[:, :3]
        u, v = caps[:, 1] - caps[:, 0], caps[:, 2] - caps[:, 0]
        cross = u[:, 0] * v[:, 1] - u[:, 1] * v[:, 0]
        caps = np.where((cross < 0)[:, None, None], caps[:, ::-1], caps)
        lift = lambda t, z: np.concatenate([t, np.full((*t.shape[:2], 1), z)], axis=2)
        tris.extend([lift(caps[:, ::-1], 0.0), lift(caps, height)])
        for ring in (p.exterior, *p.interiors):
            c = np.asarray(ring.coords)[:, :2]
            a, b = c[:-1], c[1:]
            tris.append(np.stack([lift(a[:, None], 0.0)[:, 0], lift(b[:, None], 0.0)[:, 0], lift(b[:, None], height)[:, 0]], 1))
            tris.append(np.stack([lift(a[:, None], 0.0)[:, 0], lift(b[:, None], height)[:, 0], lift(a[:, None], height)[:, 0]], 1))
    return orient(Mesh(np.concatenate(tris)), np.eye(3))


def hollow_shell(wall: float, size: float = 30.0, height: float = 20.0) -> Mesh:
    return extrude_poly(box(0, 0, size, size).difference(box(wall, wall, size - wall, size - wall)), height)


def by_rule(findings: list[dict]) -> dict[str, dict]:
    return {f["rule"]: f for f in findings}


def test_test_block_keeps_thin_wall_advice_without_duplicating_wall_rules():
    """0.30 and 0.58 mm walls are under two lines (0.90 mm): existing thin-wall findings handle them."""
    api.load((SAMPLES / "test_block.stl").read_bytes(), "test_block")
    rules = {f["rule"] for f in api.check([0, 0, 1])["findings"]}
    assert {"too_thin_to_print", "fine_detail_rough"} <= rules
    assert not ({"wall_line_mismatch", "load_bearing_wall_thin", "uneven_wall_thickness"} & rules)


def test_1mm_shell_gets_line_width_and_load_bearing_advice():
    res = run_checks(hollow_shell(1.0), Profile.from_dict({}))
    f = by_rule(res["findings"])

    assert "wall_line_mismatch" in f
    m = f["wall_line_mismatch"]
    assert "1.35 mm prints as 3 clean lines" in m["detail"]
    assert "Arachne" in m["detail"]
    assert "0.50 mm" in m["detail"] and "2 lines" in m["detail"]
    assert m["metrics"]["clean_wall_mm"] == pytest.approx(1.35)
    assert m["metrics"]["alt_line_width_mm"] == pytest.approx(0.50)
    assert m["metrics"]["alt_lines"] == 2

    assert "load_bearing_wall_thin" in f
    lb = f["load_bearing_wall_thin"]
    assert lb["severity"] == "info"
    assert "1.6 mm" in lb["detail"]

    assert "uneven_wall_thickness" not in f
    assert not ({"too_thin_to_print", "fine_detail_rough", "single_line_wall"} & set(f))


@pytest.mark.parametrize("overrides", [{"goal": "strength"}, {"constant_load": True}])
def test_load_bearing_wall_escalates_when_strength_or_load_requested(overrides):
    res = run_checks(hollow_shell(1.0), Profile.from_dict(overrides))
    lb = by_rule(res["findings"])["load_bearing_wall_thin"]
    assert lb["severity"] == "warn"


def test_whole_line_walls_do_not_trigger_mismatch():
    # 4 x 0.45 = 1.80 mm: whole lines and above 1.6 mm load-bearing minimum -> clean.
    f_thick = by_rule(run_checks(hollow_shell(1.80), Profile.from_dict({}))["findings"])
    assert not ({"wall_line_mismatch", "load_bearing_wall_thin", "uneven_wall_thickness"} & set(f_thick))

    # 3 x 0.45 = 1.35 mm: whole lines (no mismatch), still under 1.6 mm for load-bearing.
    f_mid = by_rule(run_checks(hollow_shell(1.35), Profile.from_dict({}))["findings"])
    assert "wall_line_mismatch" not in f_mid
    assert "load_bearing_wall_thin" in f_mid


def test_wall_line_mismatch_uses_profile_real_line_width():
    # 0.2 mm nozzle (0.22 mm lines): 1.10 mm is 5 exact lines -> no mismatch; 1.0 mm is 4.5 lines -> mismatch.
    assert "wall_line_mismatch" not in by_rule(run_checks(hollow_shell(1.10), Profile.from_dict({"nozzle": "0.2"}))["findings"])
    f_02 = by_rule(run_checks(hollow_shell(1.0), Profile.from_dict({"nozzle": "0.2"}))["findings"])
    assert "wall_line_mismatch" in f_02
    assert "1.10 mm prints as 5 clean lines" in f_02["wall_line_mismatch"]["detail"]
    assert "0.20 mm" in f_02["wall_line_mismatch"]["detail"] and "5 lines" in f_02["wall_line_mismatch"]["detail"]

    # 0.6 mm nozzle (0.62 mm lines): 1.0 mm is under 2 lines (no mismatch); 1.24 mm is 2 exact lines; 1.5 mm is 2.4 lines.
    assert "wall_line_mismatch" not in by_rule(run_checks(hollow_shell(1.0), Profile.from_dict({"nozzle": "0.6"}))["findings"])
    assert "wall_line_mismatch" not in by_rule(run_checks(hollow_shell(1.24), Profile.from_dict({"nozzle": "0.6"}))["findings"])

    f_06 = by_rule(run_checks(hollow_shell(1.5), Profile.from_dict({"nozzle": "0.6"}))["findings"])
    assert "wall_line_mismatch" in f_06
    assert "1.86 mm prints as 3 clean lines" in f_06["wall_line_mismatch"]["detail"]
    assert "0.75 mm" in f_06["wall_line_mismatch"]["detail"]


def test_uneven_thick_next_to_thin_warns_without_false_positives():
    # 20x20 mm thick block directly joined to a 1.6 mm thin wall, 12 mm tall.
    mixed = extrude_poly(box(0, 0, 20, 20).union(box(20, 0, 60, 1.6)), 12.0)
    f = by_rule(run_checks(mixed, Profile.from_dict({}))["findings"])
    assert "uneven_wall_thickness" in f
    u = f["uneven_wall_thickness"]
    assert u["severity"] == "info"
    assert u["metrics"]["thin_mm"] == pytest.approx(1.6, abs=0.1)
    assert u["metrics"]["thick_mm"] >= 16.0
    assert "sink" in u["detail"] or "warp" in u["detail"]

    # Same two shapes separated by a gap (not next to each other) -> should not fire.
    separate = extrude_poly(box(0, 0, 20, 20).union(box(30, 0, 70, 1.6)), 12.0)
    assert "uneven_wall_thickness" not in by_rule(run_checks(separate, Profile.from_dict({}))["findings"])

    # Normal uniform-wall parts (l_bracket, tray) must not trigger uneven_wall_thickness.
    corpus = Path(__file__).resolve().parent / "corpus"
    for path in (SAMPLES / "l_bracket.stl", corpus / "tray.stl"):
        api.load(path.read_bytes(), path.stem)
        up = api.orientations()["orientations"][0]["up"]
        assert "uneven_wall_thickness" not in by_rule(api.check(up)["findings"])


def test_new_rules_have_slicer_or_process_fixes():
    rules = load_data("rules")["rules"]
    for key in ("wall_line_mismatch", "load_bearing_wall_thin", "uneven_wall_thickness"):
        assert key in rules
        domains = {fix["domain"] for fix in rules[key]["fixes"]}
        assert domains & {"slicer", "process"}
        assert rules[key].get("sources")
