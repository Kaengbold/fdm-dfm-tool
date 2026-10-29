from pathlib import Path

import numpy as np
import pytest

from fdmcore import api
from fdmcore.mesh import Mesh

SAMPLES = Path(__file__).resolve().parents[2] / "web" / "samples"


def design(name, up, **profile):
    api.load((SAMPLES / f"{name}.stl").read_bytes(), name)
    return api.design(up, profile)


def test_holes_detected():
    r = design("l_bracket", [0, 0, 1])
    holes = sorted((f["diameter"], round(abs(f["axis"][2]))) for f in r["features"] if f["type"] == "hole")
    assert holes == [(pytest.approx(6.0, abs=0.05), 1), (pytest.approx(8.0, abs=0.05), 0)]


def test_teardrop_only_for_horizontal_holes():
    lying = design("l_bracket", [0, 0, 1])["edits"]
    assert [e["id"] for e in lying if e["id"].startswith("teardrop")] == ["teardrop-8"]
    # Printing with X up turns the Ø8 hole vertical and the Ø6 hole horizontal.
    upright = design("l_bracket", [1, 0, 0])["edits"]
    assert [e["title"] for e in upright if e["id"].startswith("teardrop")] == ["Teardrop for the horizontal Ø6.0 mm hole"]


def pieces(edit):
    tris = np.frombuffer(edit["mesh"], "<f4").reshape(-1, 3, 3).astype(float)
    return np.split(tris, np.cumsum(edit["pieces"])[:-1])


@pytest.mark.parametrize("name, material", [("l_bracket", "abs"), ("mushroom", "pla"), ("test_block", "pla")])
def test_edit_pieces_are_closed_and_outward(name, material):
    edits = design(name, [0, 0, 1], material=material)["edits"]
    assert edits
    for e in edits:
        for p in pieces(e):
            assert Mesh(p).volume() > 0, e["id"]
            # Closed and consistently wound: every directed edge a->b has a matching b->a.
            pts = [tuple(v) for v in p.reshape(-1, 3).round(4)]
            directed = {(pts[3 * f + k], pts[3 * f + (k + 1) % 3]) for f in range(len(p)) for k in range(3)}
            assert all((b, a) in directed for a, b in directed), e["id"]


def test_rounded_bed_corners_for_warping_or_big_footprints():
    from shapely.geometry import box

    from fdmcore import api
    from fdmcore.additions import extrude
    from fdmcore.mesh import Mesh, to_binary_stl

    # PLA hardly warps: a small footprint gets no rounding, a 60 mm one does; ABS always.
    api.load(to_binary_stl(Mesh(extrude(box(0, 0, 20, 20), 0, 5))), "small")
    assert not any(e["rule"] == "sharp_bed_corners" for e in api.design([0, 0, 1], {"material": "pla"})["edits"])
    assert sum(e["rule"] == "sharp_bed_corners" for e in design("l_bracket", [0, 0, 1], material="pla")["edits"]) == 4
    edits = [e for e in design("l_bracket", [0, 0, 1], material="abs")["edits"] if e["rule"] == "sharp_bed_corners"]
    assert len(edits) == 4 and all(len(e["pieces"]) == 8 for e in edits)


def test_bottom_chamfer_and_fillet_at_the_weak_spot():
    from pathlib import Path

    from fdmcore import api
    from fdmcore.booleans import apply_edits

    api.load((Path(__file__).resolve().parents[2] / "web" / "samples" / "mushroom.stl").read_bytes(), "m")
    prof = {"material": "pla"}
    edits = {e["id"]: e for e in api.design([0, 0, -1], prof)["edits"]}
    assert "foot-chamfer" in edits and "stress-chamfer" not in edits  # no strength check yet
    part = api._require().mesh
    # 0.5 mm 45° chamfer along the 160 mm cap outline: 0.125 mm² × 160 mm.
    assert abs(part.volume()) - abs(apply_edits(part, [edits["foot-chamfer"]]).volume()) == pytest.approx(20, rel=0.05)
    api.regions()
    api.strength([0, 1, 0], [{"region": 8, "kind": "fixed"}, {"region": 0, "kind": "force", "kg": 10, "direction": "pull"}], prof)
    fillet = next(e for e in api.design([0, 0, -1], prof)["edits"] if e["id"] == "stress-chamfer")
    assert fillet["op"] == "add" and len(fillet["pieces"]) == 4  # the four edges where the stem meets the cap


def test_edit_params_change_cutters():
    from pathlib import Path

    import numpy as np

    from fdmcore import api

    api.load((Path(__file__).resolve().parents[2] / "web" / "samples" / "l_bracket.stl").read_bytes(), "l")
    prof = {"material": "abs"}

    def apex(angle):
        r = api.design([0, 0, 1], prof, {"teardrop_angle_deg": angle})
        tear = next(e for e in r["edits"] if e["id"].startswith("teardrop"))
        return np.frombuffer(tear["mesh"], "<f4").reshape(-1, 3)[:, 2].max(), r

    pointy, r30 = apex(30)
    classic, _ = apex(45)
    assert pointy > classic  # a narrower tip sits higher: r/sin(30°) = 2r vs 1.41r
    assert r30["params"]["teardrop_angle_deg"] == 30
    chamfers = [e["title"] for e in api.design([0, 0, 1], prof, {"chamfer_mm": 1.5})["edits"] if e["id"].startswith("chamfer")]
    assert chamfers and all("1.5 mm" in t for t in chamfers)
    assert api.design([0, 0, 1], prof, {"chamfer_mm": 99})["params"]["chamfer_mm"] == 10  # clamped


def _apply(ids_prefix, profile=None, params=None):
    """Apply the suggested edits whose id starts with ``ids_prefix``, reload, return rules found."""
    from fdmcore.booleans import apply_edits
    from fdmcore.mesh import to_binary_stl

    edits = [e for e in api.design([0, 0, 1], profile or {}, params)["edits"] if e["id"].startswith(ids_prefix)]
    assert edits, ids_prefix
    api.load(to_binary_stl(apply_edits(api._session.mesh, edits)), "edited")
    return [f["rule"] for f in api.check([0, 0, 1], profile or {}, "both")["findings"]], edits


def counterbore_plate():
    m3d = pytest.importorskip("manifold3d")
    from fdmcore.mesh import to_binary_stl

    M = m3d.Manifold
    part = M.cube([30, 30, 8]) - M.cylinder(20, 1.7, 1.7, 48).translate([15, 15, -5])
    part = part - M.cylinder(3.5, 3.25, 3.25, 48).translate([15, 15, -0.01])  # M3 head pocket on the bed side
    m = part.to_mesh()
    return to_binary_stl(Mesh(m.vert_properties[:, :3][m.tri_verts].astype(float)))


def test_ledge_fill_removes_support():
    pytest.importorskip("manifold3d")
    design("mushroom", [0, 0, 1])
    rules, edits = _apply("ledge")
    assert edits[0]["op"] == "add" and "15 mm ledge" in edits[0]["title"]
    assert "support_marks" not in rules and "too_thin_to_print" not in rules
    # A narrower limit leaves the 15 mm ledge alone.
    design("mushroom", [0, 0, 1])
    assert not any(e["id"].startswith("ledge") for e in api.design([0, 0, 1], {}, {"ledge_max_mm": 10})["edits"])


def test_sacrificial_layer_over_counterbore():
    api.load(counterbore_plate(), "counterbore")
    assert "hole_starts_midair" in {f["rule"] for f in api.check([0, 0, 1], {}, "print")["findings"]}
    rules, edits = _apply("sacrificial")
    assert "Ø3.4 mm" in edits[0]["title"]
    assert "hole_starts_midair" not in rules


def test_thicken_thin_walls():
    pytest.importorskip("manifold3d")
    design("test_block", [0, 0, 1])
    rules, edits = _apply("wall")
    assert len(edits) == 2  # the 0.6 mm fin and the 0.3 mm one
    assert not {"too_thin_to_print", "fine_detail_rough", "single_line_wall"} & set(rules)


def test_rejected_hole_gets_no_edit_and_no_findings():
    r = design("l_bracket", [0, 0, 1])
    sideways = next(f for f in r["features"] if f["type"] == "hole" and abs(f["axis"][2]) < 0.1)
    assert sideways["confidence"] == "high" and sideways["tag"] is None
    assert "sideways_hole_oval" in {f["rule"] for f in api.check([0, 0, 1], {}, "both")["findings"]}
    feats = api.tag_feature(sideways["region"], "none")["features"]
    assert next(f for f in feats if f["region"] == sideways["region"])["tag"] == "none"
    assert not any(e["id"].startswith("teardrop") for e in api.design([0, 0, 1])["edits"])
    assert "sideways_hole_oval" not in {f["rule"] for f in api.check([0, 0, 1], {}, "both")["findings"]}
    api.tag_feature(sideways["region"], None)  # undo
    assert any(e["id"].startswith("teardrop") for e in api.design([0, 0, 1])["edits"])
    with pytest.raises(ValueError):
        api.tag_feature(sideways["region"], "rib")
