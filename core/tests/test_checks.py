import io
import json
import zipfile
from pathlib import Path

import numpy as np
import pytest

from fdmcore import api
from fdmcore.checks import orient, run_checks
from fdmcore.mesh import load_stl
from fdmcore.profile import Profile, load_data
from fdmcore.slicer import section, slice_mesh

SAMPLES = Path(__file__).resolve().parents[2] / "web" / "samples"
PKG = Path(__file__).resolve().parents[1] / "fdmcore"


def sample(name):
    return load_stl((SAMPLES / f"{name}.stl").read_bytes())


def rules_for(name, up=(0, 0, 1), **profile):
    api.load((SAMPLES / f"{name}.stl").read_bytes(), name)
    return {f["rule"]: f for f in api.check(list(up), profile, "both")["findings"]}


def test_section_area_with_hole():
    m = orient(sample("l_bracket"), np.eye(3))
    # Base: 60 x 30 plate with a Ø6 hole.
    assert section(m.triangles, 1.0).area == pytest.approx(1800 - np.pi * 9, rel=2e-3)


def test_slice_sampling_caps_layers():
    m = orient(sample("hook"), np.eye(3))
    layers = slice_mesh(m, 0.2, max_layers=50)
    assert len(layers) == 50
    assert layers[0].z == pytest.approx(0.1, abs=1e-3)


def test_mushroom_support_marks_and_flip():
    assert "support_marks" in rules_for("mushroom")
    assert "support_marks" not in rules_for("mushroom", up=(0, 0, -1))


def test_test_block_findings():
    found = rules_for("test_block")
    assert {"too_thin_to_print", "fine_detail_rough", "bridge_sag"} <= set(found)
    # 23 mm is within PLA's comfortable bridge length: it sags a little, it isn't a "long bridge".
    assert found["bridge_sag"]["metrics"]["span"] == pytest.approx(23, abs=1.5)
    assert "long_bridge" not in found
    assert "long_bridge" in rules_for("test_block", material="abs")


def test_hook_upright_is_tippy():
    found = rules_for("hook")
    assert {"tipping_risk", "support_marks"} <= set(found)


def test_warping_depends_on_material_and_enclosure():
    found = rules_for("l_bracket", material="abs", printer="prusa_mk4s")
    assert len(found["warping"]["metrics"]["points"]) == 4
    assert found["warping"]["severity"] == "problem"
    assert "warping" not in rules_for("l_bracket", material="pla")
    heated = rules_for("l_bracket", material="abs", printer="bambu_h2d")
    assert heated.get("warping", {}).get("severity") != "problem"


def test_profile_findings():
    found = rules_for("hook", up=(1, 0, 0), printer="bambu_p1s", material="pa_cf")
    assert {"hardened_nozzle_required", "dry_filament"} <= set(found)
    assert "hardened_nozzle_required" not in rules_for("hook", up=(1, 0, 0), printer="bambu_x1c", material="pa_cf")
    assert "enclosure_recommended" in rules_for("hook", up=(1, 0, 0), printer="prusa_mk4s", material="abs")


def test_modes_filter_fixes():
    api.load((SAMPLES / "mushroom.stl").read_bytes(), "mushroom")
    for mode, allowed in api.MODES.items():
        r = api.check([0, 0, 1], {}, mode)
        assert all(fx["domain"] in allowed for f in r["findings"] for fx in f["fixes"])
    assert api.check([0, 0, 1], {}, "design")["settings"] == []


def test_settings_follow_findings():
    api.load((SAMPLES / "mushroom.stl").read_bytes(), "mushroom")
    upright = {s["key"]: s["value"] for s in api.check([0, 0, 1])["settings"]}
    flipped = {s["key"]: s["value"] for s in api.check([0, 0, -1])["settings"]}
    assert upright["enable_support"] == "1" and flipped["enable_support"] == "0"


def test_ironing_and_precise_wall():
    from fdmcore.export3mf import prusa_settings
    from fdmcore.recommend import recommend_settings

    p = Profile.from_dict({})
    plain = {s["key"] for s in recommend_settings(p, [], {"top_flat_mm2": 50})}
    assert not plain & {"ironing_type", "precise_outer_wall"}
    keys = {s["key"]: s["value"] for s in recommend_settings(p, [{"rule": "gap_fuses"}], {"top_flat_mm2": 900})}
    assert keys["ironing_type"] == "topmost" and keys["precise_outer_wall"] == "1"
    assert "ironing_type" not in {s["key"] for s in recommend_settings(Profile.from_dict({"goal": "speed"}), [], {"top_flat_mm2": 900})}
    assert prusa_settings({"ironing_type": "topmost"}) == {"ironing": "1", "ironing_type": "topmost"}
    assert run_checks(orient(sample("l_bracket"), np.eye(3)), p)["stats"]["top_flat_mm2"] > 0


def test_brim_ears_when_only_corners_lift():
    from fdmcore.export3mf import prusa_settings
    from fdmcore.recommend import recommend_settings

    p = Profile.from_dict({})
    brim = lambda findings: {s["key"]: s["value"] for s in recommend_settings(p, findings)}["brim_type"]
    assert brim([{"rule": "warping"}]) == "brim_ears"
    # A small footprint or a tall part needs the full brim all round, warping or not.
    assert brim([{"rule": "warping"}, {"rule": "tipping_risk"}]) == "outer_only"
    assert brim([]) == "no_brim"
    assert prusa_settings({"brim_type": "brim_ears"}) == {"brim_type": "outer_only"}


def test_scarf_seam_for_round_parts():
    from shapely.geometry import Point, box

    from fdmcore.additions import extrude
    from fdmcore.mesh import Mesh
    from fdmcore.recommend import recommend_settings

    p = Profile.from_dict({})
    for shape, scarf in ((Point(0, 0).buffer(10, quad_segs=32), True), (box(0, 0, 20, 20), False)):
        r = run_checks(orient(Mesh(extrude(shape, 0, 10)), np.eye(3)), p)
        keys = {s["key"] for s in recommend_settings(p, r["findings"], r["stats"])}
        assert ("seam_slope_type" in keys) == scarf


def test_nut_trap_and_insert_wall():
    from shapely.geometry import Point, Polygon

    from fdmcore.additions import extrude
    from fdmcore.mesh import Mesh

    def plate(af, insert_x):
        hexagon = Polygon([(8 + af / np.sqrt(3) * np.cos(a), 10 + af / np.sqrt(3) * np.sin(a)) for a in np.arange(6) * np.pi / 3])
        insert = Point(insert_x, 10).buffer(2.0, quad_segs=16)  # Ø4.0: an M3 insert hole
        return orient(Mesh(extrude(Polygon([(0, 0), (30, 0), (30, 20), (0, 20)]).difference(hexagon).difference(insert), 0, 6)), np.eye(3))

    p = Profile.from_dict({})
    found = {f["rule"]: f for f in run_checks(plate(5.5, 27.0), p)["findings"]}
    assert found["nut_trap_tight"]["metrics"]["fastener"] == "M3"
    assert found["insert_wall_thin"]["metrics"]["wall_mm"] == pytest.approx(1.0, abs=0.05)
    loose = {f["rule"] for f in run_checks(plate(5.8, 22.0), p)["findings"]}
    assert not loose & {"nut_trap_tight", "insert_wall_thin"}


def test_screw_wall():
    from shapely.geometry import Point, Polygon

    from fdmcore.additions import extrude
    from fdmcore.mesh import Mesh

    def plate(x):  # a Ø2.5 hole: pilot size for an M3 screw cut into the plastic
        shape = Polygon([(0, 0), (30, 0), (30, 20), (0, 20)]).difference(Point(x, 10).buffer(1.25, quad_segs=16))
        return orient(Mesh(extrude(shape, 0, 6)), np.eye(3))

    p = Profile.from_dict({})
    found = {f["rule"]: f for f in run_checks(plate(27.75), p)["findings"]}
    assert found["screw_wall_thin"]["metrics"]["fastener"] == "M3"
    assert found["screw_wall_thin"]["metrics"]["wall_mm"] == pytest.approx(1.0, abs=0.05)
    assert "insert_wall_thin" not in found
    assert "screw_wall_thin" not in {f["rule"] for f in run_checks(plate(15), p)["findings"]}


def test_export_3mf():
    api.load((SAMPLES / "hook.stl").read_bytes(), "hook")
    data = api.export([1, 0, 0])
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        model = z.read("3D/3dmodel.model").decode()
        config = z.read("Metadata/model_settings.config").decode()
    assert model.count("<triangle ") == load_stl((SAMPLES / "hook.stl").read_bytes()).face_count
    assert 0 < model.count("<vertex ") < 3 * model.count("<triangle ")  # indexed, i.e. welded
    assert 'key="enable_support" value="0"' in config


def test_report_markdown():
    api.load((SAMPLES / "test_block.stl").read_bytes(), "test_block")
    md = api.report([0, 0, 1])
    assert md.startswith("# Print check: test_block")
    assert "Recommended slicer settings" in md


def test_dispatch():
    info = api.dispatch("load", json.dumps({"name": "m"}), (SAMPLES / "mushroom.stl").read_bytes())
    assert info["faces"] == 28
    assert "printers" in api.dispatch("presets")


def test_knowledge_base_covers_all_rules():
    from fdmcore.checks import RULE_IMPACT

    rules = load_data("rules")["rules"]
    assert set(RULE_IMPACT) <= set(rules)
    for rid, rule in rules.items():
        domains = {f["domain"] for f in rule["fixes"]}
        assert domains <= {"geometry", "slicer", "process"}, rid
        assert domains & {"slicer", "process"}, f"{rid} has no fix for print-only users"


def test_presets_complete():
    p = load_data("presets")
    for mid in p["materials"]:
        for pid in p["printers"]:
            prof = Profile(printer_id=pid, material_id=mid)
            assert 0 < prof.z_strength_factor <= 1


def test_manifest_lists_all_engine_files():
    listed = set(json.loads((PKG / "manifest.json").read_text()))
    actual = {str(p.relative_to(PKG)).replace("\\", "/") for p in PKG.rglob("*") if p.suffix in (".py", ".yaml")}
    actual -= {"mcp_server.py", "booleans.py", "slicer_cli.py", "cli.py"}  # local mode only
    assert listed == actual, "update core/fdmcore/manifest.json (see tools/make_manifest.py)"


def _bar(length, width, angle_deg=0.0):
    from fdmcore.mesh import Mesh, to_binary_stl

    x, y = length / 2, width / 2
    c, s = np.cos(np.radians(angle_deg)), np.sin(np.radians(angle_deg))
    v = np.array([[sx * x, sy * y, z] for sx in (-1, 1) for sy in (-1, 1) for z in (0, 10)])
    v[:, :2] = v[:, :2] @ np.array([[c, s], [-s, c]])
    quads = [(0, 2, 3, 1), (4, 5, 7, 6), (0, 1, 5, 4), (2, 6, 7, 3), (0, 4, 6, 2), (1, 3, 7, 5)]
    tris = [(a, b, c_) for a, b, c_, d in quads] + [(a, c_, d) for a, b, c_, d in quads]
    return to_binary_stl(Mesh(v[np.array(tris)][:, ::-1]))


def test_build_volume():
    api.load(_bar(240, 20), "bar")
    rules = lambda printer: {f["rule"] for f in api.check([0, 0, 1], {"printer": printer})["findings"]}  # noqa: E731
    assert "exceeds_build_volume" in rules("bambu_a1_mini")
    assert "exceeds_build_volume" not in rules("bambu_p1s")


def test_turned_to_fit():
    # 248 x 20 bar at 60°: its bounding box is 225 mm deep, too deep for a 250 x 210 bed until turned straight.
    api.load(_bar(248, 20, 60), "bar")
    found = {f["rule"]: f for f in api.check([0, 0, 1], {"printer": "prusa_mk4s"})["findings"]}
    assert "turned_to_fit" in found and "exceeds_build_volume" not in found
    o = api.orientations({"printer": "prusa_mk4s"})["orientations"][0]
    assert any("Turned" in r for r in o["reasons"])


def test_cad_face_ids_replace_guessed_regions():
    data = (SAMPLES / "hook.stl").read_bytes()
    n = load_stl(data).face_count
    api.load(data, "hook.step", face_ids=[i // 2 for i in range(n)])  # pretend every 2 triangles are one CAD face
    regions = api.regions()["regions"]
    assert len(regions) == (n + 1) // 2
    with pytest.raises(ValueError):
        api.load(data, "hook.step", face_ids=[0, 1])


def test_detail_plate_small_features():
    found = rules_for("detail_plate")
    assert {"hole_closes", "gap_fuses", "small_hole_undersized", "first_layer_detail", "weak_upright_pin"} <= set(found)
    holes = sorted(f["metrics"]["min_d"] for f in api.check([0, 0, 1], {}, "both")["findings"] if f["rule"] == "small_hole_undersized")
    assert holes == [pytest.approx(3.0, abs=0.1), pytest.approx(5.0, abs=0.1)]
    # A 0.2 mm nozzle can print the Ø1 hole.
    assert "hole_closes" not in rules_for("detail_plate", nozzle="0.2")


def test_neck_and_free_pins():
    assert "thin_neck" in rules_for("dumbbell")
    # Ribs between the enclosure's vent slots join material above: not pins.
    assert "weak_upright_pin" not in rules_for("enclosure")


def test_every_finding_has_an_impact():
    from fdmcore.checks import IMPACT_ORDER

    for name in ("detail_plate", "hook", "test_block"):
        api.load((SAMPLES / f"{name}.stl").read_bytes(), name)
        assert all(f["impact"] in IMPACT_ORDER for f in api.check([0, 0, 1], {}, "both")["findings"])


def test_compare_nozzles():
    api.load((SAMPLES / "detail_plate.stl").read_bytes(), "detail_plate")
    rows = {r["nozzle"]: r for r in api.compare_nozzles([0, 0, 1])}
    assert rows["0.8"]["print_time_min"] < rows["0.2"]["print_time_min"]
    assert len(rows["0.8"]["issues"]) > len(rows["0.2"]["issues"])
    assert rows["0.4"]["current"]
    assert not any(i["new"] for i in rows["0.2"]["issues"]) and any(i["new"] for i in rows["0.8"]["issues"])


def test_bigger_nozzles_lose_thin_detail():
    """The test block's thin fins print rough with a 0.2 mm nozzle and vanish with a 0.8; plain
    outside corners (rounded a little by any nozzle) don't count as lost detail."""
    api.load((SAMPLES / "test_block.stl").read_bytes(), "test_block")
    rows = {r["nozzle"]: r for r in api.compare_nozzles([0, 0, 1])}
    assert rows["0.2"]["lost_mm3"] == 0 < rows["0.2"]["rough_mm3"]
    assert rows["0.8"]["lost_mm3"] > rows["0.4"]["lost_mm3"] > 0
    api.load((SAMPLES / "l_bracket.stl").read_bytes(), "l_bracket")
    assert all(r["lost_mm3"] == r["rough_mm3"] == 0 for r in api.compare_nozzles([0, 0, 1]))


def test_nozzles_keep_the_wall_thickness():
    """A fair nozzle comparison: same shell thickness, each nozzle's own line width (3 × 0.45 ≈ 2 × 0.82)."""
    from fdmcore.profile import Profile
    from fdmcore.recommend import goal_plan

    thick = {}
    for nozzle in ("0.2", "0.4", "0.6", "0.8"):
        p = Profile.from_dict({"nozzle": nozzle})
        thick[nozzle] = goal_plan(p)["walls"] * p.line_width
    assert goal_plan(Profile.from_dict({"nozzle": "0.8"}))["walls"] == 2
    assert all(abs(t - thick["0.4"]) < 0.35 for t in thick.values()), thick


def test_goal_changes_settings():
    api.load((SAMPLES / "hook.stl").read_bytes(), "hook")
    walls = lambda goal: {s["key"]: s["value"] for s in api.check([1, 0, 0], {"goal": goal})["settings"]}["wall_loops"]  # noqa: E731
    assert int(walls("strength")) > int(walls("speed"))


def test_sideways_holes():
    """The L-bracket's Ø8 hole lies sideways when upright: oval, not a generic bridge-sag tip."""
    found = rules_for("l_bracket")
    assert found["sideways_hole_oval"]["metrics"]["diameter"] == pytest.approx(8, abs=0.1)
    assert "bridge_sag" not in found
    # Detail plate on its side: the Ø1 hole closes, Ø3 prints undersized, Ø5 sags.
    api.load((SAMPLES / "detail_plate.stl").read_bytes(), "detail_plate")
    out = api.check([0, 1, 0], {}, "both")["findings"]
    sideways = {f["rule"]: f["metrics"]["diameter"] for f in out if "axis" in f["metrics"]}
    assert sideways == pytest.approx({"hole_closes": 1.0, "small_hole_undersized": 3.0, "sideways_hole_oval": 5.0}, abs=0.1)


def test_teardrop_edit_clears_sideways_hole():
    pytest.importorskip("manifold3d")
    from fdmcore.booleans import apply_edits
    from fdmcore.mesh import to_binary_stl

    api.load((SAMPLES / "l_bracket.stl").read_bytes(), "l_bracket")
    api.load(to_binary_stl(apply_edits(api._session.mesh, api.design([0, 0, 1])["edits"])), "l_bracket_dfm")
    assert "sideways_hole_oval" not in {f["rule"] for f in api.check([0, 0, 1], {}, "both")["findings"]}


def test_fastener_sized_holes_get_hole_sizes():
    found = rules_for("l_bracket")  # Ø6 vertical hole = M6 nominal
    f = found["small_hole_undersized"]
    assert f["metrics"]["fastener"] == "M6" and "Ø6.6 mm" in f["detail"] and "Ø8.0 mm" in f["detail"]


def test_shrinkage_on_long_abs_part():
    long_bar = api.load(to_stl_box([200, 20, 10]), "bar")
    assert long_bar["size_mm"][0] == 200
    abs_ = {f["rule"]: f for f in api.check([0, 0, 1], {"material": "abs"}, "print")["findings"]}
    assert abs_["shrinkage_size_error"]["metrics"]["short_mm"] == pytest.approx(1.6)
    assert abs_["shrinkage_size_error"]["severity"] == "warn"
    pla = {f["rule"] for f in api.check([0, 0, 1], {"material": "pla"}, "print")["findings"]}
    assert "shrinkage_size_error" in pla  # 0.6 mm at 0.3 %


def to_stl_box(size):
    from fdmcore.mesh import Mesh, to_binary_stl

    v = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], float) * size
    f = [[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]]
    return to_binary_stl(Mesh(v[np.array(f)]))


@pytest.mark.parametrize("material", ["pla_plus", "asa", "pc", "tpu_95a"])
def test_new_materials_run(material):
    found = rules_for("enclosure", material=material, printer="creality_k1c")
    assert isinstance(found, dict)


def test_sideways_hole_vs_sphere_cavity():
    """A hollow sphere cuts as circles too, but it isn't a hole; a through hole crossed by a
    vertical hole is still one hole."""
    m3d = pytest.importorskip("manifold3d")
    from fdmcore.holes import horizontal_holes
    from fdmcore.mesh import Mesh

    M = m3d.Manifold
    part = M.cube([80, 30, 40]) - M.sphere(10, 96).translate([60, 15, 25])
    part = part - M.cylinder(100, 3, 3, 96).rotate([0, 90, 0]).translate([-10, 15, 8])
    part = part - M.cylinder(100, 2, 2, 64).translate([30, 15, -10])
    mesh = part.to_mesh()
    tri = mesh.vert_properties[:, :3][mesh.tri_verts].astype(float)
    found = horizontal_holes(orient(Mesh(tri), np.eye(3)))
    assert [round(h["diameter"], 1) for h in found] == [6.0]


@pytest.mark.parametrize("length, through, trapped", [(40, False, True), (30, True, False), (100, True, True)])
def test_support_trapped_in_blind_or_long_sideways_hole(length, through, trapped):
    """Ø20 sideways hole in PETG (bridges ≤ 15 mm) needs support inside; it's stuck if the hole
    is blind or a long tunnel."""
    m3d = pytest.importorskip("manifold3d")
    from fdmcore.mesh import Mesh, to_binary_stl

    M = m3d.Manifold
    block_x = length if through else length + 20
    hole = M.cylinder(length + (20 if through else 0), 10, 10, 128).rotate([0, 90, 0])
    part = M.cube([block_x, 40, 40]) - hole.translate([-10 if through else -1, 20, 20])
    mesh = part.to_mesh()
    api.load(to_binary_stl(Mesh(mesh.vert_properties[:, :3][mesh.tri_verts].astype(float))), "block")
    found = {f["rule"]: f for f in api.check([0, 0, 1], {"material": "petg"}, "print")["findings"]}
    assert ("trapped_support" in found) == trapped, found.get("trapped_support", {}).get("detail")


@pytest.mark.parametrize("depth, width, trapped", [(40, 20, True), (15, 20, False)])
def test_support_trapped_in_deep_side_pocket(depth, width, trapped):
    """A 20 mm wide slot into the side of a block needs support in PETG (bridges ≤ 15 mm); 40 mm deep
    it can't be reached to pull out, 15 mm deep it can."""
    m3d = pytest.importorskip("manifold3d")
    from fdmcore.mesh import Mesh, to_binary_stl

    M = m3d.Manifold
    part = M.cube([60, 40, 30]) - M.cube([depth + 1, width, 10]).translate([-1, 20 - width / 2, 8])
    mesh = part.to_mesh()
    api.load(to_binary_stl(Mesh(mesh.vert_properties[:, :3][mesh.tri_verts].astype(float))), "pocket")
    found = {f["rule"]: f for f in api.check([0, 0, 1], {"material": "petg"}, "print")["findings"]}
    assert ("trapped_support" in found) == trapped
    assert ("support_marks" in found) != trapped


def test_custom_presets_and_calibration():
    """A user's own material, based on ABS but calibrated on their printer: the measured warping
    length replaces the built-in one, and bad values are refused."""
    from fdmcore.mesh import load_stl

    try:
        api.custom_presets(materials={"abs_x": {"based_on": "abs", "name": "My ABS", "warp_length_mm": {"none": 600}}},
                           printers={"box": {"based_on": "bambu_a1", "name": "My printer", "bed": [300, 300, 300]}})
        assert api.get_presets()["materials"]["my_abs_x"]["name"] == "My ABS"
        api.load(to_stl_box([200, 20, 10]), "bar")
        builtin = {f["rule"] for f in api.check([0, 0, 1], {"material": "abs", "printer": "bambu_a1"}, "print")["findings"]}
        mine = {f["rule"] for f in api.check([0, 0, 1], {"material": "my_abs_x", "printer": "my_box"}, "print")["findings"]}
        assert "warping" in builtin and "warping" not in mine
        with pytest.raises(ValueError, match="between"):
            api.custom_presets(materials={"bad": {"based_on": "pla", "max_overhang_deg": 89}})
        with pytest.raises(ValueError, match="based_on"):
            api.custom_presets(materials={"bad": {"based_on": "unobtainium"}})
    finally:
        api.custom_presets()  # back to built-ins only
    assert "my_abs_x" not in api.get_presets()["materials"]
    for kind in ("layer_strength", "warping"):
        part = load_stl(api.calibration_part(kind)["stl"])
        assert part.volume() > 1000


def test_heat_build_up_flags_a_thin_pin_not_a_block():
    from shapely.geometry import Point, box

    from fdmcore.additions import extrude
    from fdmcore.checks import run_checks
    from fdmcore.mesh import Mesh
    from fdmcore.profile import Profile

    prof = Profile.from_dict({"material": "pla", "printer": "bambu_p1s"})
    pin = Mesh(np.concatenate([extrude(box(0, 0, 20, 20), 0, 2), extrude(Point(10, 10).buffer(1.5, 32), 2, 22)]))
    block = Mesh(extrude(box(0, 0, 40, 40), 0, 20))
    hot = [f for f in run_checks(pin, prof)["findings"] if f["rule"] == "small_layers_cooling"]
    assert len(hot) == 1 and hot[0]["severity"] == "warn" and hot[0]["bbox"][0][2] >= 2.0
    r = run_checks(block, prof)
    assert not [f for f in r["findings"] if f["rule"] == "small_layers_cooling"]
    heat = r["stats"]["layer_heat"]
    assert len(heat["z"]) == len(heat["temp_c"]) and max(heat["temp_c"]) < prof.material["nozzle_temp_c"]


def test_shallow_raised_and_engraved_details():
    from shapely.geometry import box

    from fdmcore.additions import extrude
    from fdmcore.checks import run_checks
    from fdmcore.mesh import Mesh
    from fdmcore.profile import Profile

    prof = Profile.from_dict({})  # 0.2 mm layers
    plate = box(0, 0, 40, 20)
    raised = [box(5, 5, 8, 15), box(10, 5, 13, 15)]  # two "letters" 0.2 mm high
    deep = box(25, 5, 35, 15)  # pocket 0.6 mm deep: fine
    body = extrude(plate.difference(deep), 0, 3)
    floor = extrude(deep, 0, 2.4)
    letters = [extrude(r, 3, 3.2) for r in raised]
    # One closed solid isn't needed for the check: it looks at the up-facing faces.
    mesh = Mesh(np.concatenate([body, floor, *letters]))
    found = [f for f in run_checks(mesh, prof)["findings"] if f["rule"] == "shallow_relief"]
    assert len(found) == 1 and found[0]["severity"] == "warn" and found[0]["metrics"]["pieces"] == 2
    assert "Raised" in found[0]["detail"]
    shallow = Mesh(np.concatenate([extrude(plate.difference(deep), 0, 3), extrude(deep, 0, 2.7)]))
    eng = [f for f in run_checks(shallow, prof)["findings"] if f["rule"] == "shallow_relief"]
    assert len(eng) == 1 and "Engraved" in eng[0]["detail"] and eng[0]["severity"] == "warn"


def test_overhang_follows_the_material_angle():
    """A block leaning out along one edge: within PLA's 55° no support, well past it support (T-172:
    edge strips used to count as short bridges and the angle got half a line width extra)."""
    from shapely.geometry import Polygon

    from fdmcore.additions import extrude
    from fdmcore.checks import run_checks
    from fdmcore.mesh import Mesh
    from fdmcore.profile import Profile

    prof = Profile.from_dict({"material": "pla"})

    def leaning(deg):
        side = Polygon([(0, 0), (8, 0), (8 + 15 * np.tan(np.radians(deg)), 15), (0, 15)])
        return Mesh(extrude(side, 0, 8)[:, :, [0, 2, 1]][:, ::-1])

    rules = lambda deg: {f["rule"] for f in run_checks(leaning(deg), prof)["findings"]}
    assert "support_marks" not in rules(50)
    assert "support_marks" in rules(65)


def test_small_or_deep_text_on_an_upright_face():
    from shapely.geometry import box

    from fdmcore.additions import extrude
    from fdmcore.checks import run_checks
    from fdmcore.mesh import Mesh
    from fdmcore.profile import Profile

    prof = Profile.from_dict({})
    wall = extrude(box(0, 0, 40, 5), 0, 20)  # 40 mm wide, 5 mm thick, 20 mm tall; front face at y = 0

    def letters(z0, z1, depth):
        """Raised "letters" standing out of the front face (y < 0)."""
        return [extrude(box(x, -depth, x + 3, 0), z0, z1) for x in (5, 12, 19)]

    found = lambda m: [f for f in run_checks(Mesh(np.concatenate(m)), prof)["findings"] if f["rule"] == "side_detail_small"]
    small = found([wall, *letters(8, 10, 0.6)])  # 2 mm tall
    assert len(small) == 1 and "tall" in small[0]["detail"] and small[0]["metrics"]["pieces"] == 3
    assert not found([wall, *letters(6, 12, 0.6)])  # 6 mm tall: fine


def test_thick_blocks_without_a_buffer():
    """Shrinking a layer by 8 mm with a buffer killed the engine in the browser ("depth mismatch" in
    Pyodide's older GEOS) on an L-shaped layer 2–6 mm thick and on a rounded square (T-268). The thick
    core is now found on a grid with distances only, and matches the buffer's answer."""
    from shapely.geometry import MultiPolygon, Point, Polygon, box

    from fdmcore.checks import _thick_core

    layer = MultiPolygon([
        Polygon([(10.4777, 21.5), (10.4777, 16.5), (-23.5, 16.5), (-23.5, -3.6592), (-25.5, -3.6592), (-25.5, -9.3566),
                 (-23.5, -9.3566), (-23.5, -18.5), (-25.5, -18.5), (-25.5, -21.5), (-31.5, -21.5), (-31.5, 21.5)]),
        Polygon([(31.5, 21.5), (31.5, 16.5), (12.5223, 16.5), (12.5223, 21.5)])])
    assert _thick_core(layer, 8.0) == (0.0, None)
    # Exact openings: a 40 mm square loses its corners to 8 mm rounds; a thin strip on it vanishes; a disc stays.
    square = 40 * 40 - (4 - np.pi) * 8 ** 2
    for shape, exact, where in ((box(0, 0, 40, 40), square, (0, 0, 40, 40)),
                                (box(0, 0, 40, 40).union(box(40, 0, 90, 3)), square, (0, 0, 40, 40)),
                                (Point(0, 0).buffer(11, quad_segs=16), np.pi * 11 ** 2, (-11, -11, 11, 11))):
        area, bounds = _thick_core(shape, 8.0)
        assert area == pytest.approx(exact, rel=0.1)
        assert bounds == pytest.approx(where, abs=1.0)


def test_too_big_counts_as_can_fail():
    api.load((SAMPLES / "hook.stl").read_bytes(), "hook")
    api.scale(20)
    f = next(f for f in api.check([0, 0, 1], {}, "both")["findings"] if f["rule"] == "exceeds_build_volume")
    assert f["impact"] == "fails"


def test_markdown_cells_keep_the_table_together():
    assert api.md_cell("a | b\nc") == r"a \| b c"


def test_cost_support_and_spool():
    """Price per kg and grams left on the spool from My materials: cost of the print with its support,
    and a warning when the spool won't last."""
    try:
        api.custom_presets(materials={"cheap": {"based_on": "pla", "name": "Cheap PLA", "price_per_kg": 20, "spool_left_g": 30}})
        api.load((SAMPLES / "mushroom.stl").read_bytes(), "mushroom")
        stats = api.check([0, 0, 1], {"material": "my_cheap"}, "print")["stats"]
        assert stats["support_g"] > 0  # the cap overhangs: support under it
        assert stats["total_g"] == pytest.approx(stats["filament_g"] + stats["support_g"], abs=0.2)
        assert stats["cost"] == pytest.approx(stats["total_g"] * 0.02, abs=0.01)
        assert stats["spool_short"] == (stats["total_g"] * 1.1 + 5 > 30)
        plain = api.check([0, 0, 1], {"material": "pla"}, "print")["stats"]
        assert "cost" not in plain and "spool_short" not in plain
    finally:
        api.custom_presets()


def test_each_orientation_says_its_support_grams():
    """Orientation cards show support grams (and the app the cost from them): upside down, the mushroom
    needs no support under its cap; standing on its stem it does."""
    api.load((SAMPLES / "mushroom.stl").read_bytes(), "mushroom")
    r = api.orientations({"material": "pla"})
    assert all(o["support_g"] >= 0 for o in [*r["orientations"], r["as_loaded"]])
    assert r["orientations"][0]["support_g"] < r["as_loaded"]["support_g"]
    one = api.orientation([0, 0, 1], {"material": "pla"})
    assert one["support_g"] == pytest.approx(r["as_loaded"]["support_g"], rel=0.3)


def _roof(deg: float, nozzle_lh: float | None = None):
    """40 × 40 mm block, 10 mm high, with a roof rising ``deg`` from flat along x."""
    from shapely.geometry import Polygon

    from fdmcore.additions import extrude
    from fdmcore.mesh import Mesh

    rise = 40 * np.tan(np.radians(deg))
    return Mesh(extrude(Polygon([(0, 0), (40, 0), (40, 10 + rise), (0, 10)]), 0, 40)[..., [0, 2, 1]][:, ::-1])


def test_shallow_roof_gets_thinner_layers_in_the_3mf():
    """A 10° roof prints as ~1.1 mm wide steps at 0.2 mm layers (0.45 mm at 0.08 mm); the 3MF carries the
    thinner layers over just the roof's height for OrcaSlicer / Bambu Studio and PrusaSlicer."""
    from fdmcore.mesh import to_binary_stl

    api.load(to_binary_stl(_roof(10)), "roof")
    f = [f for f in api.check([0, 0, 1], {})["findings"] if f["rule"] == "shallow_slope_stairs"]
    assert len(f) == 1 and f[0]["impact"] == "surface"
    m = f[0]["metrics"]
    assert (m["z0"], m["z1"], m["layer_height"]) == (pytest.approx(10, abs=0.1), pytest.approx(17.05, abs=0.1), 0.08)
    assert m["step_mm"] == pytest.approx(1.13, abs=0.02) and m["fine_step_mm"] == pytest.approx(0.45, abs=0.02)
    z = zipfile.ZipFile(io.BytesIO(api.export([0, 0, 1], {}, {}, fine_layers=[m])))
    for name in ("Metadata/layer_config_ranges.xml", "Metadata/Prusa_Slicer_layer_config_ranges.xml"):
        assert '<range min_z="10.000" max_z="17.050">' in z.read(name).decode().replace("17.040", "17.050").replace("17.060", "17.050")


def test_steep_flat_and_tiny_slopes_are_left_alone():
    from fdmcore.mesh import to_binary_stl

    for deg in (0, 35):  # flat top: ironing; 35°: steps narrower than 0.3 mm
        api.load(to_binary_stl(_roof(deg)), "roof")
        assert not [f for f in api.check([0, 0, 1], {})["findings"] if f["rule"] == "shallow_slope_stairs"], deg
    api.load(to_binary_stl(_roof(10)), "roof")  # a 0.2 mm nozzle prints 0.1 mm layers: nothing thinner to gain
    assert not [f for f in api.check([0, 0, 1], {"nozzle": "0.2"})["findings"] if f["rule"] == "shallow_slope_stairs"]


def test_seam_kept_off_faces_marked_clean():
    """Faces marked "clean" (seen) are painted as seam blockers in the 3MF, under OrcaSlicer's / Bambu Studio's
    and PrusaSlicer's names, in both layouts (tools/slicer_check.py checks that all three keep the seam off)."""
    from fdmcore.mesh import Mesh, to_binary_stl
    from test_formats import box

    api.load(to_binary_stl(Mesh(box([0, 0, 0], 1) * 20)), "cube")
    regions = api.regions()["regions"]
    side = next(r["id"] for r in regions if abs(r["normal"][0]) > 0.99)
    for zones, model in ((None, "3D/3dmodel.model"), ([{"lo": [5, 5, 5], "hi": [10, 10, 10], "walls": 4, "infill": 0.5}], "3D/3dmodel.model")):
        xml = zipfile.ZipFile(io.BytesIO(api.export([0, 0, 1], {}, {}, zones, faces={"clean": [side]}))).read(model).decode()
        assert xml.count('paint_seam="8" slic3rpe:custom_seam="8"') == 2 and "xmlns:slic3rpe=" in xml  # the face's two triangles
    plain = zipfile.ZipFile(io.BytesIO(api.export([0, 0, 1], {}, {}))).read("3D/3dmodel.model").decode()
    assert "paint_seam" not in plain
