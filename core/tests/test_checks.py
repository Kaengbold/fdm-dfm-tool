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
    actual -= {"mcp_server.py", "booleans.py", "slicer_cli.py"}  # local mode only
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
