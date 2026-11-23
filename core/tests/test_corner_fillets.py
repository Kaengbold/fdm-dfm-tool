"""Tests for T-440: inside-corner foot fillets and print-friendly chamfers without a strength run."""

from pathlib import Path

import numpy as np
import pytest
from shapely.geometry import box

from fdmcore import api
from fdmcore.mesh import Mesh, mesh_health, to_binary_stl
from fdmcore.profile import load_data

ROOT = Path(__file__).resolve().parents[2]
SAMPLES = ROOT / "web" / "samples"
CORPUS = Path(__file__).parent / "corpus"


def _extrude_xz(boxes: list, depth: float) -> Mesh:
    """Closed prism of a 2D (y, z) profile extruded along X, centred on the bed."""
    import shapely
    from shapely.geometry.polygon import orient as orient_poly
    from shapely.ops import unary_union

    from fdmcore.checks import orient

    poly = orient_poly(unary_union(boxes).simplify(0), 1.0)
    caps = shapely.get_coordinates(
        shapely.get_parts(shapely.constrained_delaunay_triangles(poly))
    ).reshape(-1, 4, 2)[:, :3]
    u, v = caps[:, 1] - caps[:, 0], caps[:, 2] - caps[:, 0]
    caps = np.where((u[:, 0] * v[:, 1] - u[:, 1] * v[:, 0] < 0)[:, None, None], caps[:, ::-1], caps)
    lift = lambda t, x: np.concatenate([np.full((*t.shape[:2], 1), x), t], axis=2)
    ring = np.asarray(poly.exterior.coords)
    a, b = ring[:-1], ring[1:]
    walls = [
        np.stack([lift(a[:, None], 0)[:, 0], lift(b[:, None], 0)[:, 0], lift(b[:, None], depth)[:, 0]], 1),
        np.stack([lift(a[:, None], 0)[:, 0], lift(b[:, None], depth)[:, 0], lift(a[:, None], depth)[:, 0]], 1),
    ]
    tris = np.concatenate([lift(caps[:, ::-1], 0), lift(caps, depth), *walls])
    mesh = orient(Mesh(tris), np.eye(3))
    return Mesh(mesh.triangles[:, ::-1]) if mesh.volume() < 0 else mesh


@pytest.mark.parametrize("profile, phrase", [
    ({}, "matters more for clips"),
    ({"repeated_load": True}, "worth trying here"),
    ({"repeated_load": False}, "helps a little"),
])
def test_tall_pin_offers_foot_fillet_without_strength(profile, phrase):
    """tall_pin gets one foot fillet advice at z=3 without running strength (no duplicate for both pin findings)."""
    r_expected = float(load_data("thresholds")["fillets"]["foot_radius_mm"])
    api.load((CORPUS / "tall_pin.stl").read_bytes(), "tall_pin")
    up = [0, 0, 1]
    chk = api.check(up)
    assert any(f["rule"] == "weak_upright_pin" for f in chk["findings"])

    des = api.design(up, profile, findings=chk["findings"])
    fillets = [a for a in des["advice"] if a["rule"] == "foot_fillet"]
    assert len(fillets) == 1, fillets
    a = fillets[0]
    assert f"r {r_expected:.1f} mm" in a["text"]
    assert "foot" in a["text"] and "pin" in a["text"]
    assert phrase in a["text"] and "load runs along the layers" in a["text"]
    assert a["target"] == pytest.approx(r_expected)
    assert a["unit"] == "mm"
    assert a["fixes"]["rule"] in ("weak_upright_pin", "tall_thin_feature")
    assert abs(a["point"][2] - 3.0) < 0.5


def test_dumbbell_neck_offers_foot_fillet_without_strength():
    """dumbbell upright reports thin_neck and offers a foot fillet at z=15 without duplicating ledge-0 at z=21."""
    r_expected = float(load_data("thresholds")["fillets"]["foot_radius_mm"])
    api.load((SAMPLES / "dumbbell.stl").read_bytes(), "dumbbell")
    up = [0, 0, 1]
    chk = api.check(up)
    assert any(f["rule"] == "thin_neck" for f in chk["findings"])

    des = api.design(up, findings=chk["findings"])
    fillets = [a for a in des["advice"] if a["rule"] == "foot_fillet"]
    assert len(fillets) == 1, fillets
    a = fillets[0]
    assert f"r {r_expected:.1f} mm" in a["text"]
    assert "neck" in a["text"] and "foot" in a["text"]
    assert a["target"] == pytest.approx(r_expected)
    assert a["fixes"]["rule"] == "thin_neck"
    assert abs(a["point"][2] - 15.0) < 0.5

    # Without a ledge fill covering the overhanging top of the neck (z=21), the overhanging corner
    # gets a 45 deg chamfer instead of a round, explicitly saying "prints without support".
    des_no_ledge = api.design(up, params={"ledge_max_mm": 2.0}, findings=chk["findings"])
    fillets2 = [a for a in des_no_ledge["advice"] if a["rule"] == "foot_fillet"]
    assert len(fillets2) == 2, fillets2
    chamfers = [a for a in fillets2 if "45° chamfer" in a["text"]]
    assert len(chamfers) == 1, fillets2
    assert "prints without support" in chamfers[0]["text"]
    assert abs(chamfers[0]["point"][2] - 21.0) < 0.5


def test_snap_fit_gets_clean_foot_fillet_edit_and_keeps_hook_advice():
    """An upright snap tab with straight foot edges gets both a clean edit and advice without dropping hook advice."""
    tab = [box(0, 0, 10, 5), box(4, 5, 5.5, 15), box(4, 15, 5.5, 17), box(5.5, 15, 6.5, 17)]
    mesh = _extrude_xz(tab, 10.0)
    api.load(to_binary_stl(mesh), "snap_tab")
    up = [0, 0, 1]
    chk = api.check(up)
    assert any(f["rule"] == "snap_fit_upright" for f in chk["findings"])

    des = api.design(up, findings=chk["findings"])
    edits = [e for e in des["edits"] if e["rule"] == "foot_fillet"]
    assert len(edits) == 1, edits
    e = edits[0]
    assert e["source"] == "finding" and e["fixes"]["rule"] == "snap_fit_upright"
    assert "grows_mm" not in e

    # Both the foot-fillet advice (linked to the edit) and the hook-depth advice stay present.
    assert any(a["rule"] == "foot_fillet" and a["edit_id"] == e["id"] for a in des["advice"])
    assert any(a["rule"] == "snap_fit_upright" for a in des["advice"])

    pytest.importorskip("manifold3d")
    from fdmcore.booleans import apply_edits, inspect_mesh

    part = api._require().mesh
    lo0, hi0 = part.bounds()
    out = apply_edits(part, [e])
    info = inspect_mesh(out)
    health = mesh_health(out)
    assert info["closed"] and info["pieces"] == 1 and info["volume"] > inspect_mesh(part)["volume"]
    assert health["boundary_edges"] == 0 and health["non_manifold_edges"] == 0 and health["flipped_faces"] == 0
    lo1, hi1 = out.bounds()
    assert np.allclose(lo1, lo0, atol=0.02) and np.allclose(hi1, hi0, atol=0.02)


def test_overhanging_neck_corner_offers_45_chamfer_edit_and_advice():
    """A straight-walled neck whose top overhang exceeds ledge_max_mm gets a round at its foot and a
    45 deg chamfer ('prints without support') at its overhanging top, both as clean edits."""
    # Profile in (y, z): 50 mm wide bottom, 6 mm neck, 50 mm wide top (22 mm overhang > default 20 mm ledge_max).
    t_neck = [box(0, 0, 50, 15), box(22, 15, 28, 21), box(0, 21, 50, 36)]
    mesh = _extrude_xz(t_neck, 10.0)
    api.load(to_binary_stl(mesh), "t_neck")
    up = [0, 0, 1]
    chk = api.check(up)
    assert any(f["rule"] == "thin_neck" for f in chk["findings"])

    des = api.design(up, findings=chk["findings"])
    edits = [e for e in des["edits"] if e["rule"] == "foot_fillet"]
    assert len(edits) == 2, edits
    rounds = [e for e in edits if "round" in e["title"].lower()]
    chamfers = [e for e in edits if "45° chamfer" in e["title"].lower()]
    assert len(rounds) == 1 and len(chamfers) == 1, edits
    assert "prints without support" in chamfers[0]["description"]

    adv_chamfer = next(a for a in des["advice"] if a["edit_id"] == chamfers[0]["id"])
    assert "45° chamfer" in adv_chamfer["text"] and "prints without support" in adv_chamfer["text"]

    pytest.importorskip("manifold3d")
    from fdmcore.booleans import apply_edits, inspect_mesh

    part = api._require().mesh
    lo0, hi0 = part.bounds()
    out = apply_edits(part, edits)
    info = inspect_mesh(out)
    health = mesh_health(out)
    assert info["closed"] and info["pieces"] == 1 and info["volume"] > inspect_mesh(part)["volume"]
    assert health["boundary_edges"] == 0 and health["non_manifold_edges"] == 0 and health["flipped_faces"] == 0
    lo1, hi1 = out.bounds()
    assert np.allclose(lo1, lo0, atol=0.02) and np.allclose(hi1, hi0, atol=0.02)


def test_no_duplication_with_boss_ribs_or_strength_chamfer():
    """Pins already reinforced by tall_boss ribs or strength stress-chamfer get no duplicate foot_fillet."""
    # detail_plate has a weak_upright_pin already covered by boss-ribs-5.
    api.load((SAMPLES / "detail_plate.stl").read_bytes(), "detail_plate")
    des = api.design([0, 0, 1])
    assert any(e["rule"] == "tall_boss" and e["source"] == "finding" for e in des["edits"])
    assert not any(a["rule"] == "foot_fillet" for a in des["advice"])
    assert not any(e["rule"] == "foot_fillet" for e in des["edits"])

    # dumbbell upright after a strength run that adds stress-chamfer at the neck foot (z=15).
    api.load((SAMPLES / "dumbbell.stl").read_bytes(), "dumbbell")
    regs = api.regions()["regions"]
    bot = min((r for r in regs if r["normal"][2] < -0.9), key=lambda r: r["centroid"][2])
    top = max((r for r in regs if r["normal"][2] > 0.9), key=lambda r: r["centroid"][2])
    loads = [
        {"region": bot["id"], "kind": "fixed"},
        {"region": top["id"], "kind": "force", "kg": 50, "direction": "pull"},
    ]
    st = api.strength([0, 0, 1], loads)
    assert st["verdict"] != "fine"
    des_s = api.design([0, 0, 1], {"repeated_load": True})
    stress_edit = next(e for e in des_s["edits"] if e["id"] == "stress-chamfer")
    assert "worth trying here" in stress_edit["description"]
    ideas = api.strength_options([0, 0, 1], loads, {"repeated_load": False})
    fillet_idea = next(o for o in ideas if o.get("edit") == "stress-chamfer")
    assert "helps a little" in fillet_idea["why"] and fillet_idea["safety_factor"] is None
    # The corner covered by stress-chamfer must not also be emitted as a foot_fillet.
    stress_pts = np.frombuffer(stress_edit["mesh"], "<f4").reshape(-1, 3)
    s_lo, s_hi = stress_pts.min(axis=0), stress_pts.max(axis=0)
    for a in des_s["advice"]:
        if a["rule"] == "foot_fillet":
            pt = np.asarray(a["point"], float)
            assert not (np.all(pt >= s_lo - 2.0) and np.all(pt <= s_hi + 2.0)), (a, s_lo, s_hi)
