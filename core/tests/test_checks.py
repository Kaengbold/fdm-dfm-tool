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


def test_mushroom_overhang_and_flip():
    assert "unsupported_overhang" in rules_for("mushroom")
    assert "unsupported_overhang" not in rules_for("mushroom", up=(0, 0, -1))


def test_test_block_findings():
    found = rules_for("test_block")
    assert {"too_thin_to_print", "thin_wall", "long_bridge"} <= set(found)
    assert found["long_bridge"]["metrics"]["span"] == pytest.approx(23, abs=1.5)
    assert "unsupported_overhang" not in found


def test_hook_upright_is_tippy():
    found = rules_for("hook")
    assert {"tipping_risk", "unsupported_overhang"} <= set(found)


def test_bed_corners_counted():
    found = rules_for("l_bracket", material="abs")
    assert len(found["sharp_bed_corners"]["metrics"]["points"]) == 4
    assert found["sharp_bed_corners"]["severity"] == "warn"


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
    rules = load_data("rules")["rules"]
    emitted = {"unsupported_overhang", "long_bridge", "short_bridge", "thin_wall", "too_thin_to_print",
               "small_bed_contact", "sharp_bed_corners", "tipping_risk", "tall_thin_feature", "small_layers_cooling",
               "warping_material", "enclosure_recommended", "hardened_nozzle_required", "dry_filament",
               "larger_nozzle_option", "elephant_foot"}
    assert emitted <= set(rules)
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
    actual -= {"mcp_server.py", "booleans.py"}  # local mode only
    assert listed == actual, "update core/fdmcore/manifest.json (see tools/make_manifest.py)"
