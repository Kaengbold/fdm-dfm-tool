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
    assert len(edits) == 4 and all(len(e["pieces"]) == 1 for e in edits)  # one fan-shaped cutter per corner


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


def _clean(tris: np.ndarray) -> tuple[int, int]:
    """(edges not shared by exactly two triangles, collapsed triangles) once the result is saved as a
    float STL and its corners are matched by position, the way a slicer reads it. Collapsed: two
    corners at the same spot, or no area at all (tiny but proper triangles are fine)."""
    t = tris.astype("<f4")
    _, ids = np.unique(t.reshape(-1, 3), axis=0, return_inverse=True)
    ids = ids.reshape(-1, 3)
    edges = np.sort(np.concatenate([ids[:, [0, 1]], ids[:, [1, 2]], ids[:, [2, 0]]]), axis=1)
    _, counts = np.unique(edges, axis=0, return_counts=True)
    t = t.astype(float)
    area = np.linalg.norm(np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0]), axis=1) / 2
    same = (ids[:, 0] == ids[:, 1]) | (ids[:, 1] == ids[:, 2]) | (ids[:, 0] == ids[:, 2])
    return int((counts != 2).sum()), int((same | (area < 1e-10)).sum())


@pytest.mark.parametrize("name, material", [("l_bracket", "abs"), ("test_block", "abs"), ("hook", "pla"),
                                            ("detail_plate", "pla"), ("dumbbell", "pla"), ("phone_stand", "abs")])
def test_all_edits_together_give_a_clean_mesh(name, material):
    """Every suggested change applied at once (cuts and fills meeting at corners): the result must
    reach the slicer without mesh errors. Cutters used to touch the part's own corner edges exactly,
    leaving zero-thickness fins and loose crumbs."""
    pytest.importorskip("manifold3d")
    from fdmcore.booleans import apply_edits

    api.load((SAMPLES / f"{name}.stl").read_bytes(), name)
    prof = {"material": material}
    up = api.orientations(prof)["orientations"][0]["up"]
    edits = api.design(up, prof)["edits"]
    assert len(edits) >= 4
    assert _clean(apply_edits(api._require().mesh, edits).triangles) == (0, 0)


def test_thicker_weak_spot_helps():
    """A bar clamped at one end and pulled down at the other breaks at the clamp: "make the weak spot
    thicker" adds plastic round it (tapering out), and the strength check gets better on the result."""
    import numpy as np

    from fdmcore import api
    from fdmcore.booleans import apply_edits
    from fdmcore.mesh import to_binary_stl
    from test_hold import box, region_facing

    prof = {"material": "pla"}

    def loads():
        api.regions()
        return [{"region": region_facing([-1, 0, 0]), "kind": "fixed"}, {"region": region_facing([1, 0, 0]), "kind": "force", "kg": 4, "direction": "down"}]

    api.load(box(80, 12, 12), "bar")
    before = api.strength([0, 0, 1], loads(), prof)
    spot = np.asarray(api._require().weak_spot[0])
    assert spot[0] < 15  # at the clamp
    edit = next(e for e in api.design([0, 0, 1], prof, {"thicken_mm": 2})["edits"] if e["id"] == "thicken-weak")
    assert edit["op"] == "add" and edit["param"] == "thicken_mm" and "2.0 mm" in edit["title"]
    pts = np.frombuffer(edit["mesh"], "<f4").reshape(-1, 3)
    # All round the bar along the stretch bent hardest (stress at least 65 % of the clamp's: the first third),
    # tapering out well before the loaded end.
    assert pts[:, 0].max() < 60 and pts[:, 1].max() > 13 and pts[:, 2].max() > 13
    assert any(o.get("edit") == "thicken-weak" for o in api.strength_options([0, 0, 1], loads(), prof))
    part = api._require().mesh
    thicker = apply_edits(part, [edit])
    assert abs(thicker.volume()) > abs(part.volume())
    api.load(to_binary_stl(thicker), "bar2")
    after = api.strength([0, 0, 1], loads(), prof)
    assert after["safety_factor"] > before["safety_factor"] * 1.1


def test_chamfers_at_every_weak_corner():
    """Two equal posts pushed sideways are equally weak at their roots. A chamfer at only the worst one
    just moves the weak spot to the other post (2.07 -> 2.04); both together help (chamfers -> 2.85,
    the rounds these upright corners now get -> 2.58)."""
    manifold3d = pytest.importorskip("manifold3d")
    from fdmcore.booleans import apply_edits
    from fdmcore.mesh import to_binary_stl

    cube = manifold3d.Manifold.cube
    m = (cube((60, 20, 5)) + cube((8, 8, 20)).translate((6, 6, 5)) + cube((8, 8, 20)).translate((46, 6, 5))).to_mesh()
    prof = {"material": "pla"}

    def check(mesh_bytes):
        api.load(mesh_bytes, "posts")
        info = api.regions()["regions"]
        face = lambda n, ok: [r["id"] for r in info if np.dot(r["normal"], n) > 0.99 and ok(r["centroid"])]  # noqa: E731
        loads = [{"region": face([0, 0, -1], lambda c: True)[0], "kind": "fixed"}]
        loads += [{"region": f, "kind": "force", "kg": 3, "direction": "push"} for f in face([0, -1, 0], lambda c: c[2] > 6)]
        return api.strength([0, 0, 1], loads, prof)["safety_factor"]

    before = check(to_binary_stl(Mesh(m.vert_properties[m.tri_verts.reshape(-1)].reshape(-1, 3, 3).astype(float))))
    edit = next(e for e in api.design([0, 0, 1], prof)["edits"] if e["id"] == "stress-chamfer")
    pts = np.frombuffer(edit["mesh"], "<f4").reshape(-1, 3)
    assert "2 inside corners" in edit["title"] and pts[:, 0].min() < 10 and pts[:, 0].max() > 50  # both posts
    after = check(to_binary_stl(apply_edits(api._require().mesh, [edit])))
    assert after > 1.2 * before
def test_loose_pieces_after_edits_are_dropped():
    # An addition that ends up apart from the part (two changes meeting at a corner, a taper cut
    # short) would print as a loose crumb; one that overlaps the part joins it.
    pytest.importorskip("manifold3d")
    from shapely.geometry import box

    from fdmcore.additions import extrude
    from fdmcore.booleans import apply_edits

    part = Mesh(extrude(box(0, 0, 20, 20), 0, 10))
    joined, loose = extrude(box(18, 5, 25, 15), 0, 5), extrude(box(30, 5, 32, 7), 0, 2)
    pieces = np.concatenate([joined, loose]).astype("<f4")
    edit = {"id": "test", "op": "add", "mesh": pieces.tobytes(), "pieces": [len(joined), len(loose)]}
    out = apply_edits(part, [edit])
    assert abs(out.volume()) == pytest.approx(20 * 20 * 10 + 5 * 10 * 5)
    assert out.triangles[..., 0].max() == pytest.approx(25)


def test_thicken_only_the_neck_of_a_flat_dog_bone():
    """A flat dog-bone pulled apart is weakest at its narrow neck (the stress peaks where it meets the
    wide ends). Thickening grows the neck only, never below the bed, and helps; growing all round the
    worst spot made the ends wider, the part lift off the bed and the strength worse (1.33 -> 0.09)."""
    manifold3d = pytest.importorskip("manifold3d")
    from fdmcore.booleans import apply_edits
    from fdmcore.mesh import to_binary_stl

    cube = manifold3d.Manifold.cube
    m = (cube((20, 40, 6)).translate((0, -20, 0)) + cube((40, 12, 6)).translate((20, -6, 0))
         + cube((20, 40, 6)).translate((60, -20, 0))).to_mesh()
    prof = {"material": "pla"}

    def check(data):
        api.load(data, "bone")
        info = api.regions()["regions"]
        end = lambda n, ok: next(r["id"] for r in info if np.dot(r["normal"], n) > 0.99 and ok(r["centroid"][0]))  # noqa: E731
        loads = [{"region": end([-1, 0, 0], lambda x: x < 1), "kind": "fixed"},
                 {"region": end([1, 0, 0], lambda x: x > 79), "kind": "force", "kg": 60, "direction": "pull"}]
        return api.strength([0, 0, 1], loads, prof)["safety_factor"]

    before = check(to_binary_stl(Mesh(m.vert_properties[m.tri_verts.reshape(-1)].reshape(-1, 3, 3).astype(float))))
    edit = next(e for e in api.design([0, 0, 1], prof)["edits"] if e["id"] == "thicken-weak")
    out = apply_edits(api._require().mesh, [edit])
    t = out.triangles.reshape(-1, 3)
    head = (t[:, 0] < 18) | (t[:, 0] > 62)
    neck = (t[:, 0] > 30) & (t[:, 0] < 50)
    assert t[:, 2].min() > -0.01  # still flat on the bed
    assert np.abs(t[head, 1]).max() < 20.01 and t[head, 2].max() < 6.01  # the wide ends stay as they are
    assert np.abs(t[neck, 1]).max() > 7 and t[neck, 2].max() > 7  # the neck is thicker
    assert check(to_binary_stl(out)) > 1.05 * before


def test_round_where_it_prints_chamfer_where_it_would_hang():
    """The weak corner where the mushroom's stem meets the cap: printed cap down, the corner opens upwards
    and gets a round; printed cap up, a round under the cap would hang over, so it is a 45° chamfer."""
    api.load((SAMPLES / "mushroom.stl").read_bytes(), "m")
    api.regions()
    prof = {"material": "pla"}
    api.strength([0, 1, 0], [{"region": 8, "kind": "fixed"}, {"region": 0, "kind": "force", "kg": 10, "direction": "pull"}], prof)
    cap_down = next(e for e in api.design([0, 0, -1], prof)["edits"] if e["id"] == "stress-chamfer")
    cap_up = next(e for e in api.design([0, 0, 1], prof)["edits"] if e["id"] == "stress-chamfer")
    assert "round" in cap_down["title"] and "chamfer" not in cap_down["title"]
    assert "chamfer" in cap_up["title"] and "round" not in cap_up["title"]
    # The round fills about as much as the chamfer would (just as wide, it would be about half).
    tris = lambda e: np.frombuffer(e["mesh"], "<f4").reshape(-1, 3, 3).astype(float)  # noqa: E731
    assert Mesh(tris(cap_down)).volume() == pytest.approx(Mesh(tris(cap_up)).volume(), rel=0.35)
