"""Tests for T-520: material environment fields, 1.0 mm nozzle, Profile layer-height override, and layer_bond_factor."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest
from shapely.geometry import box as rect

from fdmcore import api
from fdmcore.additions import extrude
from fdmcore.checks import setup_findings
from fdmcore.mesh import Mesh
from fdmcore.profile import Profile, presets, set_custom_presets
from fdmcore.recommend import goal_plan
from fdmcore.regions import find_regions
from fdmcore.strength import LAYER_BOND_CAP, Shells, evaluate, layer_bond_factor, material_for, prepare

SAMPLES = Path(__file__).resolve().parents[2] / "web" / "samples"

RATING_FIELDS = ("water", "abrasion", "oil_fuel", "solvent", "skin")
ENV_FIELDS = (*RATING_FIELDS, "food_notes")
VALID_RATINGS = {"good", "fair", "poor", None}


def test_material_environment_fields_schema_and_json():
    """Every built-in material defines the 6 environment fields with valid values or None (unknown)."""
    data = presets()
    json.dumps(data)  # presets remain JSON-friendly
    materials = data["materials"]
    assert len(materials) >= 8
    for mid, m in materials.items():
        for field in ENV_FIELDS:
            assert field in m, f"{mid} is missing {field}"
        for field in RATING_FIELDS:
            assert m[field] in VALID_RATINGS, f"{mid}.{field} = {m[field]!r} not in {VALID_RATINGS}"
        note = m["food_notes"]
        assert note is None or (isinstance(note, str) and len(note) > 20)
        if note is not None:
            low = note.lower()
            assert "not" in low, f"{mid}.food_notes must not claim raw prints are food-safe"


def test_material_environment_sourced_values_and_nulls():
    """Sourced values match primary datasheets; unverified fields stay None rather than guessed."""
    m = presets()["materials"]
    # Carbon-fibre nylon: poor skin contact (surface barbs), good wear/oil/solvent, fair water (PA6 vs PA12)
    assert m["pa_cf"]["skin"] == "poor"
    assert m["pa_cf"]["abrasion"] == "good"
    assert m["pa_cf"]["oil_fuel"] == "good"
    assert m["pa_cf"]["solvent"] == "good"
    assert m["pa_cf"]["water"] == "fair"
    # TPU 95A: high wear resistance, oil/grease resistance, ether-vs-ester water caveat
    assert m["tpu_95a"]["abrasion"] == "good"
    assert m["tpu_95a"]["oil_fuel"] == "fair"
    assert m["tpu_95a"]["water"] == "fair"
    # Prusament chemical resistance study values
    assert m["pla"]["water"] == "fair" and m["pla"]["solvent"] == "poor"
    assert m["petg"]["water"] == "good" and m["petg"]["solvent"] == "fair"
    assert m["asa"]["water"] == "good" and m["asa"]["abrasion"] == "good" and m["asa"]["solvent"] == "poor"
    assert m["pc"]["water"] == "good" and m["pc"]["solvent"] == "fair"
    assert m["abs"]["solvent"] == "poor"
    # Unknown / brand-dependent values stay null
    for field in RATING_FIELDS:
        assert m["pla_plus"][field] is None
    for mid in ("pla", "petg", "abs", "pla_plus", "asa", "pc", "tpu_95a"):
        assert m[mid]["skin"] is None


def test_custom_material_overrides_environment_fields():
    """Custom material presets can override the new environment fields including setting None."""
    try:
        set_custom_presets(materials={
            "food_petg": {
                "based_on": "petg",
                "water": "good",
                "abrasion": "fair",
                "oil_fuel": None,
                "skin": "fair",
                "food_notes": "Certified spool, still needs a food-safe liner.",
            }
        })
        custom = presets()["materials"]["my_food_petg"]
        assert custom["water"] == "good"
        assert custom["abrasion"] == "fair"
        assert custom["oil_fuel"] is None
        assert custom["skin"] == "fair"
        assert "liner" in custom["food_notes"]

        with pytest.raises(ValueError, match="water"):
            set_custom_presets(materials={"bad": {"based_on": "pla", "water": "excellent"}})
    finally:
        set_custom_presets()


def test_one_mm_nozzle_preset_and_integration():
    """The 1.0 mm nozzle preset loads cleanly and works across Profile, goal_plan, setup_check and compare_nozzles."""
    nozzles = presets()["nozzles"]
    assert "1.0" in nozzles
    assert nozzles["1.0"]["line_width"] == pytest.approx(1.02)
    assert nozzles["1.0"]["layer_height"] == pytest.approx(0.5)

    p = Profile.from_dict({"nozzle": "1.0"})
    assert p.line_width == pytest.approx(1.02)
    assert p.layer_height == pytest.approx(0.5)
    plan = goal_plan(p)
    assert plan["layer_height"] == pytest.approx(0.5)
    assert plan["walls"] == 2
    assert not setup_findings(p)

    api.load((SAMPLES / "l_bracket.stl").read_bytes(), "l_bracket")
    rows = api.compare_nozzles([0, 0, 1])
    assert "1.0" in [r["nozzle"] for r in rows]
    json.dumps(rows)


def test_profile_layer_height_override():
    """Profile accepts an explicit layer_height override and falls back to the nozzle default when omitted."""
    assert Profile(nozzle="0.4").layer_height == pytest.approx(0.2)
    assert Profile(nozzle="0.6").layer_height == pytest.approx(0.3)
    assert Profile(nozzle="1.0").layer_height == pytest.approx(0.5)

    p = Profile(nozzle="0.4", layer_height_mm=0.16)
    assert p.layer_height == pytest.approx(0.16)
    assert replace(p, use_temp_c=50.0).layer_height == pytest.approx(0.16)
    # Changing only the nozzle follows the new nozzle's default, never a stale derived height.
    assert replace(Profile(nozzle="0.4"), nozzle="1.0").layer_height == pytest.approx(0.5)
    assert replace(p, nozzle="0.6").layer_height == pytest.approx(0.16)  # an explicit override stays

    assert Profile.from_dict({"nozzle": "0.4", "layer_height": 0.12}).layer_height == pytest.approx(0.12)
    assert Profile.from_dict({"nozzle": "0.4", "layer_height": "0.28"}).layer_height == pytest.approx(0.28)
    assert Profile.from_dict({"nozzle": "0.4", "layer_height": None}).layer_height == pytest.approx(0.2)
    assert Profile.from_dict({"nozzle": "0.4", "layer_height": ""}).layer_height == pytest.approx(0.2)

    for bad in (0, -0.1, float("nan"), float("inf"), True, False):
        with pytest.raises(ValueError):
            Profile(nozzle="0.4", layer_height_mm=bad)
        with pytest.raises(ValueError):
            Profile.from_dict({"nozzle": "0.4", "layer_height": bad})
    with pytest.raises(ValueError):
        Profile.from_dict({"nozzle": "0.4", "layer_height": "thick"})
    with pytest.raises(ValueError, match="Unknown nozzle preset"):
        Profile.from_dict({"nozzle": "9.9"})


def test_layer_bond_factor_curve_shape_and_cap():
    """Bonding rises up to ~0.5–0.6 × nozzle, stays capped at 1.0, and drops sharply above ~0.7 × nozzle."""
    # Calibration baseline: every built-in nozzle's default (0.5 × nozzle) equals 1.0
    for n in ("0.2", "0.4", "0.6", "0.8", "1.0"):
        d = float(n)
        assert layer_bond_factor(0.5 * d, d) == pytest.approx(1.0)
        assert layer_bond_factor(0.5 * d, n) == pytest.approx(1.0)

    # Rises from very thin layers to the 0.5–0.6 × nozzle window
    d = 0.4
    thin = [layer_bond_factor(r * d, d) for r in (0.15, 0.25, 0.40, 0.50)]
    assert thin == sorted(thin) and thin[0] < thin[-1] == pytest.approx(1.0)
    assert layer_bond_factor(0.55 * d, d) == pytest.approx(1.0)
    assert layer_bond_factor(0.60 * d, d) == pytest.approx(1.0)

    # Capped: never exceeds LAYER_BOND_CAP (1.0) — no unsupported bonus
    for i in range(1, 31):
        r = i * 0.05
        f = layer_bond_factor(r * d, d)
        assert 0.0 < f <= LAYER_BOND_CAP

    # Falls steeply above ~0.7 × nozzle (Kuznetsov 2018 & CNC Kitchen 2019)
    f_070 = layer_bond_factor(0.70 * d, d)
    f_075 = layer_bond_factor(0.75 * d, d)
    f_085 = layer_bond_factor(0.85 * d, d)
    f_100 = layer_bond_factor(1.00 * d, d)
    assert f_070 > f_075 > f_085 > f_100 > 0
    assert 0.50 <= f_075 <= 0.75
    assert 0.25 <= f_100 <= 0.45

    # Ratio-based: same h/D gives same factor regardless of nozzle size
    for r in (0.2, 0.5, 0.75, 0.9):
        vals = [layer_bond_factor(r * float(n), n) for n in ("0.2", "0.4", "0.6", "0.8", "1.0")]
        assert vals == pytest.approx([vals[0]] * len(vals))

    # Validation
    for bad_lh, bad_d in ((0, 0.4), (-0.1, 0.4), (0.2, 0), (0.2, -0.4), (float("nan"), 0.4), (0.2, float("inf")), (True, 0.4), (0.2, False)):
        with pytest.raises(ValueError):
            layer_bond_factor(bad_lh, bad_d)


def test_layer_bond_factor_affects_material_and_strength_evaluate():
    """Overriding layer_height on Profile scales Z strength via layer_bond_factor without changing XY."""
    p_std = Profile(material_id="pla", nozzle="0.4", layer_height_mm=0.20)
    p_thin = Profile(material_id="pla", nozzle="0.4", layer_height_mm=0.08)
    p_thick = Profile(material_id="pla", nozzle="0.4", layer_height_mm=0.36)

    m_std = material_for(p_std)
    m_thin = material_for(p_thin)
    m_thick = material_for(p_thick)

    assert m_std.strength_xy == pytest.approx(m_thick.strength_xy) == pytest.approx(m_thin.strength_xy)
    assert m_std.e_xy == pytest.approx(m_thick.e_xy)
    assert m_std.strength_z == pytest.approx(p_std.material["tensile_xy_mpa"] * p_std.z_strength_factor)
    assert m_thin.strength_z < m_std.strength_z  # no bonus for ultra-thin layers
    assert m_thick.strength_z < 0.5 * m_std.strength_z  # steep drop at 0.9 × nozzle

    # End-to-end FEA check: a tall cantilever pulled horizontally puts tension across Z layers at the root
    mesh = Mesh(extrude(rect(0, 0, 10, 10), 0, 40))
    regions = find_regions(mesh)
    normals, _ = mesh.normals_and_areas()
    bottom = int(regions[normals[:, 2] < -0.9][0])
    top = int(regions[normals[:, 2] > 0.9][0])
    case = prepare(mesh, regions, [
        {"region": bottom, "kind": "fixed"},
        {"region": top, "kind": "force", "kg": 1.0, "direction": [1.0, 0.0, 0.0]},
    ], target_voxels=3000)

    shells = Shells(walls=2, infill=1.0, top=4, bottom=4)
    r_std = evaluate(case, [0, 0, 1], p_std, shells)
    r_thick = evaluate(case, [0, 0, 1], p_thick, shells)
    assert r_thick["safety_factor"] < 0.6 * r_std["safety_factor"]
