"""Cantilever snap-fit bending strain and CAD limits (T-338)."""

from pathlib import Path
import math

import numpy as np
import pytest
import shapely
from shapely.geometry import box
from shapely.geometry.polygon import orient as orient_poly
from shapely.ops import unary_union

from fdmcore import api, features
from fdmcore.checks import orient, run_checks, snap_fit_strain
from fdmcore.mesh import Mesh
from fdmcore.profile import Profile, load_data

SAMPLES = Path(__file__).resolve().parents[2] / "web" / "samples"


def _snap_mesh(length: float = 12.0, thickness: float = 1.5, hook: float = 1.0,
               upright: bool = True, width: float = 10.0, base_h: float = 5.0) -> Mesh:
    """Watertight synthetic cantilever snap tab of exact root thickness ``thickness``,
    length ``length`` above a ``base_h`` base, and ``hook`` lip at the tip."""
    hook_h = 2.0
    profile = unary_union([
        box(0, 0, 10, base_h),
        box(4, base_h, 4 + thickness, base_h + length),
        box(4 + thickness, base_h + length - hook_h, 4 + thickness + hook, base_h + length),
    ])
    poly = orient_poly(profile.simplify(0), 1.0)
    caps = shapely.get_coordinates(shapely.get_parts(shapely.constrained_delaunay_triangles(poly))).reshape(-1, 4, 2)[:, :3]
    u, v = caps[:, 1] - caps[:, 0], caps[:, 2] - caps[:, 0]
    caps = np.where((u[:, 0] * v[:, 1] - u[:, 1] * v[:, 0] < 0)[:, None, None], caps[:, ::-1], caps)
    lift = lambda t, z: np.concatenate([t, np.full((*t.shape[:2], 1), z)], axis=2)
    ring = np.asarray(poly.exterior.coords)
    a, b = ring[:-1], ring[1:]
    walls = [np.stack([lift(a[:, None], 0)[:, 0], lift(b[:, None], 0)[:, 0], lift(b[:, None], width)[:, 0]], 1),
             np.stack([lift(a[:, None], 0)[:, 0], lift(b[:, None], width)[:, 0], lift(a[:, None], width)[:, 0]], 1)]
    tris = np.concatenate([lift(caps[:, ::-1], 0), lift(caps, width), *walls])
    # Upright: profile lies in Y-Z (beam rises along +Z); flat: profile lies in X-Y (extruded along +Z).
    if upright:
        tris = tris[..., [2, 0, 1]]
    return orient(Mesh(tris), np.eye(3))


def test_snap_fit_strain_formula_upright_and_flat():
    S = load_data("thresholds")["snap_fits"]
    L, t, y = 12.0, 1.5, 1.0
    expected_strain = 150.0 * t * y / (L ** 2)  # 1.5625 %

    up = snap_fit_strain(L, t, y, "pla", S, upright=True)
    flat = snap_fit_strain(L, t, y, "pla", S, upright=False)

    assert up["strain_pct"] == pytest.approx(expected_strain, abs=0.01)
    assert flat["strain_pct"] == pytest.approx(expected_strain, abs=0.01)
    # Across layers halves PLA's 2.0 % flat allowable strain to 1.0 % (Core77 / Fictiv).
    assert up["strain_max_flat_pct"] == 2.0
    assert up["strain_max_pct"] == 1.0
    assert flat["strain_max_pct"] == 2.0
    assert up["min_length_mm"] == pytest.approx(math.sqrt(1.5 * t * y / 0.01), abs=0.1)
    assert flat["min_length_mm"] == pytest.approx(math.sqrt(1.5 * t * y / 0.02), abs=0.1)
    assert up["max_thickness_mm"] == pytest.approx(0.01 * L ** 2 / (1.5 * y), abs=0.01)
    assert flat["max_thickness_mm"] == pytest.approx(0.02 * L ** 2 / (1.5 * y), abs=0.01)
    assert up["hook_max_mm"] == pytest.approx(0.01 * L ** 2 / (1.5 * t), abs=0.01)
    assert flat["hook_max_mm"] == pytest.approx(0.02 * L ** 2 / (1.5 * t), abs=0.01)
    assert up["hook_max_mm"] == pytest.approx(up["hook_max_flat_mm"] * S["across_layers"], abs=0.01)


def test_synthetic_upright_and_flat_snap_checks_and_advice():
    L, t, y = 12.0, 1.5, 1.0
    upright_mesh = _snap_mesh(L, t, y, upright=True)

    # PLA (allow 1.0 % upright, 2.0 % flat): 1.56 % exceeds upright limit -> problem + exact CAD advice.
    pla_profile = Profile(material_id="pla")
    pla_findings = [f for f in run_checks(upright_mesh, pla_profile)["findings"] if f["rule"] == "snap_fit_upright"]
    assert len(pla_findings) == 1
    f = pla_findings[0]
    m = f["metrics"]
    assert f["severity"] == "problem"
    assert m["length_mm"] == pytest.approx(L, abs=0.3)
    assert m["thickness_mm"] == pytest.approx(t, abs=0.15)
    assert m["hook_mm"] == pytest.approx(y, abs=0.15)
    assert m["strain_pct"] == pytest.approx(150.0 * t * y / (L ** 2), abs=0.15)
    assert m["strain_max_pct"] == 1.0 and m["strain_max_flat_pct"] == 2.0
    assert "bends" in f["detail"] and "PLA allows about 1 %" in f["detail"]

    T = load_data("thresholds")
    adv = features._finding_advice(f, pla_profile, T, 20.0)
    assert adv is not None
    text, now, tgt, unit = adv
    assert unit == "mm" and now == pytest.approx(m["length_mm"], abs=0.1)
    assert tgt == pytest.approx(m["min_length_mm"], abs=0.1)
    assert f"{m['min_length_mm']:.1f} mm long" in text
    assert f"{m['max_thickness_mm']:.2f} mm thick" in text
    assert f"{m['hook_max_mm']:.2f} mm" in text

    # PETG (allow 1.75 % upright, 3.5 % flat): 1.56 % is below upright limit -> warn only, no over-strain advice.
    petg_profile = Profile(material_id="petg")
    petg_findings = [x for x in run_checks(upright_mesh, petg_profile)["findings"] if x["rule"] == "snap_fit_upright"]
    assert len(petg_findings) == 1 and petg_findings[0]["severity"] == "warn"
    assert petg_findings[0]["metrics"]["strain_max_pct"] == 1.75
    assert features._finding_advice(petg_findings[0], petg_profile, T, 20.0) is None

    # Flat on the bed: bends along layer lines, so snap_fit_upright clears.
    flat_mesh = _snap_mesh(L, t, y, upright=False)
    assert not [x for x in run_checks(flat_mesh, pla_profile)["findings"] if x["rule"] == "snap_fit_upright"]


@pytest.mark.parametrize("mat", ["pla_plus", "pa_cf", "tpu_95a"])
def test_unsourced_materials_omit_limits_and_advice(mat):
    upright_mesh = _snap_mesh(12.0, 1.5, 1.0, upright=True)
    profile = Profile(material_id=mat)
    T = load_data("thresholds")
    found = [f for f in run_checks(upright_mesh, profile)["findings"] if f["rule"] == "snap_fit_upright"]
    assert len(found) == 1
    f = found[0]
    m = f["metrics"]
    assert f["severity"] == "warn"
    assert m["strain_pct"] == pytest.approx(1.56, abs=0.15)
    for omitted in ("strain_max_pct", "strain_max_flat_pct", "hook_max_mm", "hook_max_flat_mm",
                    "min_length_mm", "max_thickness_mm"):
        assert omitted not in m
    assert features._finding_advice(f, profile, T, 20.0) is None


def test_real_sample_cable_clip_snap_strain_and_advice():
    """Real sample web/samples/cable_clip.stl standing in [0, 1, 0] has two symmetric C-clip snap arms."""
    api.load((SAMPLES / "cable_clip.stl").read_bytes(), "cable_clip")
    checked = api.check([0, 1, 0], {"material": "pla"})
    snaps = [f for f in checked["findings"] if f["rule"] == "snap_fit_upright"]
    assert len(snaps) == 2
    for f in snaps:
        m = f["metrics"]
        assert f["severity"] == "problem"
        assert 10.0 <= m["length_mm"] <= 15.0
        assert 2.0 <= m["thickness_mm"] <= 3.0
        assert 0.5 <= m["hook_mm"] <= 1.2
        assert 1.5 <= m["strain_pct"] <= 3.0
        assert m["strain_max_pct"] == 1.0
        assert m["min_length_mm"] > m["length_mm"]
        assert m["max_thickness_mm"] < m["thickness_mm"]
        assert m["hook_max_mm"] < m["hook_mm"]

    designed = api.design([0, 1, 0], {"material": "pla"}, findings=checked["findings"])
    snap_advice = [a for a in designed["advice"] if a["rule"] == "snap_fit_upright"]
    assert len(snap_advice) == 1
    assert snap_advice[0]["count"] == 2
    assert snap_advice[0]["text"].startswith("2 upright snap tabs: make at least ")
