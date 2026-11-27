from pathlib import Path

import numpy as np
import pytest

from fdmcore import api
from fdmcore.mesh import Mesh, load_stl, to_binary_stl
from fdmcore.orientation import BED, NEEDS_SUPPORT, OK, classify_faces, evaluate, find_orientations, pose_names, rotation_to_z

SAMPLES = Path(__file__).resolve().parents[2] / "web" / "samples"


def cube(size=10.0) -> Mesh:
    v = np.array([[x, y, z] for x in (0, size) for y in (0, size) for z in (0, size)], dtype=float)
    # Quads are listed inward-wound; reversed below so the cube faces outward.
    quads = [(0, 2, 3, 1), (4, 5, 7, 6), (0, 1, 5, 4), (2, 6, 7, 3), (0, 4, 6, 2), (1, 3, 7, 5)]
    tris = [(a, b, c) for a, b, c, d in quads] + [(a, c, d) for a, b, c, d in quads]
    return Mesh(v[np.array(tris)][:, ::-1])


def sample(name) -> bytes:
    return (SAMPLES / f"{name}.stl").read_bytes()


def test_pose_names_for_plate_and_tall_part():
    plate = Mesh(cube().triangles * [3, 2, 0.2])
    poses = [evaluate(plate, up) for up in ([0, 0, 1], [1, 0, 0], [0, 0, -1], [-1, 0, 0])]
    names = pose_names(plate, poses)
    assert names == ["As in your file", "Standing on the short edge", "Upside down",
                     "Standing on the short edge, turned over"]
    tall = Mesh(cube().triangles * [1, 1, 4])
    assert pose_names(tall, [evaluate(tall, [1, 0, 0])]) == ["On its big flat side"]


def test_pose_names_for_l_bracket():
    mesh = load_stl(sample("l_bracket"))
    results = [evaluate(mesh, up) for up in ([0, 0, 1], [1, 0, 0], [-1, 0, 0])]
    names = pose_names(mesh, results)
    assert names[0] == "As in your file"
    assert len(set(names)) == len(names)


def test_cube_geometry():
    m = cube()
    assert m.volume() == pytest.approx(1000.0)
    classes = classify_faces(m, [0, 0, 1])
    assert (classes == BED).sum() == 2
    assert (classes == OK).sum() == 10


def test_binary_roundtrip():
    m = cube()
    again = load_stl(to_binary_stl(m))
    assert np.allclose(again.triangles, m.triangles)


def test_binary_stl_with_solid_header_and_trailing_bytes():
    data = to_binary_stl(cube())
    data = b"solid exported".ljust(80, b" ") + data[80:] + b"\0\0"
    assert load_stl(data).face_count == 12


def test_ascii_stl():
    data = b"""solid t
facet normal 0 0 1
outer loop
vertex 0 0 0
vertex 1 0 0
vertex 0 1 0
endloop
endfacet
endsolid t"""
    assert load_stl(data).face_count == 1


def test_invalid_stl():
    with pytest.raises(ValueError):
        load_stl(b"not an stl at all" * 10)


def test_ceiling_needs_support():
    m = cube()
    # Lift the cube: the bottom faces are no longer on the bed but still point down.
    lifted = Mesh(np.vstack([m.triangles, m.triangles + [0, 0, 20]]))
    classes = classify_faces(lifted, [0, 0, 1])
    assert (classes == NEEDS_SUPPORT).sum() == 2


def test_rotation_to_z():
    for up in ([1, 0, 0], [0, 0, -1], [1, 2, 3], [0, 0, 1]):
        u = np.array(up, float) / np.linalg.norm(up)
        r = rotation_to_z(u)
        assert np.allclose(r @ u, [0, 0, 1])
        assert np.allclose(r @ r.T, np.eye(3))


@pytest.mark.skipif(not SAMPLES.exists(), reason="run tools/make_samples.py")
def test_mushroom_flipped():
    best = find_orientations(load_stl(sample("mushroom")))[0]
    assert np.allclose(best.up, [0, 0, -1], atol=1e-6)
    assert best.support_area == pytest.approx(0.0)


@pytest.mark.skipif(not SAMPLES.exists(), reason="run tools/make_samples.py")
def test_hook_on_its_side():
    best = find_orientations(load_stl(sample("hook")))[0]
    assert abs(best.up[0]) == pytest.approx(1.0)


@pytest.mark.skipif(not SAMPLES.exists(), reason="run tools/make_samples.py")
def test_api_orientations_payload():
    info = api.load(sample("l_bracket"), "l_bracket.stl")
    faces = info["faces"]
    assert len(info["triangles"]) == faces * 36
    r = api.orientations({"material": "petg"})
    for o in [r["as_loaded"], *r["orientations"]]:
        assert len(o["face_classes"]) == faces
        assert o["reasons"]


def test_l_bracket_support_text_uses_sliced_result(monkeypatch):
    from fdmcore import refine

    bridge_decisions = []
    bridgeable = refine._bridgeable

    def record_bridge(piece, anchors, limit):
        result = bridgeable(piece, anchors, limit)
        bridge_decisions.append(result)
        return result

    api.load(sample("l_bracket"), "l_bracket")
    options = api.orientations()
    best = options["orientations"][0]
    assert best["support_area_mm2"] == pytest.approx(42.613306)
    monkeypatch.setattr(refine, "_bridgeable", record_bridge)
    stats = refine.support_stats(api.orient(api._session.mesh, best["rotation"]), api.Profile.from_dict(None))
    assert bridge_decisions and all(bridge_decisions)
    assert stats["support_area"] == stats["support_volume"] == 0
    assert best["support_g"] == 0
    for option in [options["as_loaded"], *options["orientations"]]:
        assert option["support_summary"] in option["reasons"]
        assert not any(reason.startswith("Needs supports") for reason in option["reasons"])
        assert (option["support_summary"] == "No support") == (not option["support_needed"])
        assert not any(reason == "Prints without supports" for reason in option["reasons"])


@pytest.mark.parametrize("grams, needed, expected", [
    (0, False, "No support"),
    (0.001, True, "A little support (under 1 g)"),
    (0.9, True, "A little support (under 1 g)"),
    (1, True, "About 1 g support"),
])
def test_support_summary(grams, needed, expected):
    from fdmcore.orientation import support_summary

    assert support_summary(grams, needed) == expected


def test_unrefined_support_reason_stays():
    from fdmcore.orientation import evaluate

    result = evaluate(load_stl(sample("l_bracket")), [0, -1, 0])
    assert result.support_grams is None
    assert "Needs supports under about 43 mm²" in result.reasons


@pytest.mark.parametrize(
    "name, expected_up",
    [
        ("enclosure", [[0, 0, 1]]),  # open side up
        ("phone_stand", [[0, 1, 0], [0, -1, 0], [0, 0, 1], [0, 0, -1]]),  # on its base, or the profile flat (both print without supports)
        ("cable_clip", [[0, 0, 1], [0, 0, -1]]),  # flat on its side
        ("mushroom", [[0, 0, -1]]),
        ("hook", [[1, 0, 0], [-1, 0, 0]]),
    ],
)
@pytest.mark.skipif(not SAMPLES.exists(), reason="run tools/make_samples.py")
def test_typical_parts_orientation(name, expected_up):
    api.load(sample(name), name)
    options = api.orientations()["orientations"]
    assert any(np.allclose(options[0]["up"], e, atol=1e-6) for e in expected_up), options[0]["up"]
    assert all(o["bed_area_mm2"] > 0 for o in options), "options without bed contact should be hidden"


def test_score_breakdown_explains_lower_options():
    from pathlib import Path

    from fdmcore import api

    api.load((Path(__file__).resolve().parents[2] / "web" / "samples" / "hook.stl").read_bytes(), "hook")
    r = api.orientations({"goal": "strength"})
    first, *rest = r["orientations"]
    for o in [first, *rest, r["as_loaded"]]:
        assert sum(o["score_terms"].values()) == pytest.approx(o["score"], abs=0.01)
    assert "why_lower" not in first
    assert all(o["why_lower"].startswith(("Ranked lower", "About as good")) for o in rest)
    assert "support" in rest[0]["why_lower"]


def test_marked_faces_steer_orientation_and_check():
    """Mushroom: best is cap down. Marking the cap 'clean' or the stem end 'on the bed' flips it."""
    api.load(sample("mushroom"), "mushroom")
    regions = api.regions()["regions"]
    best = api.orientations()["orientations"][0]
    assert np.allclose(best["up"], [0, 0, -1], atol=1e-6)
    stem_end = min((r for r in regions if r["kind"] == "flat"), key=lambda r: r["normal"][2])["id"]  # faces -Z
    cap_top = max((r for r in regions if r["kind"] == "flat"), key=lambda r: r["normal"][2])["id"]  # faces +Z
    on_bed = api.orientations(faces={"bed": [stem_end]})["orientations"][0]
    assert np.allclose(on_bed["up"], [0, 0, 1], atol=1e-6)
    assert any("sits on the bed" in x for x in on_bed["reasons"])
    # Cap down puts its underside on supports; marking the underside clean makes that a problem.
    underside = [r["id"] for r in regions if r["normal"][2] < -0.5 and r["id"] != stem_end]
    if underside:
        found = api.check([0, 0, 1], {}, "print", faces={"clean": underside})["findings"]
        assert any(f["rule"] == "support_marks" and f["severity"] == "problem" for f in found)
    assert cap_top != stem_end


def test_upright_mark_stands_a_hole_up():
    """Marking the sideways Ø8 hole of the L-bracket "vertical" turns its axis up, so it prints round."""
    api.load((SAMPLES / "l_bracket.stl").read_bytes(), "l_bracket")
    best = api.orientations()["orientations"][0]["up"]
    hole = next(f for f in api.design(best)["features"] if f["type"] == "hole" and abs(f["axis"][2]) < 0.1)
    assert abs(np.dot(best, hole["axis"])) < 0.1  # lying: the hole is sideways
    ranked = api.orientations(faces={"vertical": [hole["region"]]})["orientations"]
    assert abs(np.dot(ranked[0]["up"], hole["axis"])) > 0.99
    assert "The face you picked stands upright" in ranked[0]["reasons"]


def test_pulled_mushroom_offers_strength_without_recommending_a_tip():
    """Pulling the stem favours its side, but that pose tips on a narrow footprint.
    T-405's bed-safety guard takes priority; still offer the stronger direction
    and explain its strength advantage instead of silently removing it.
    """
    from fdmcore import api

    api.load((SAMPLES / "mushroom.stl").read_bytes(), "m")
    api.regions()
    loads = [{"region": 8, "kind": "fixed"}, {"region": 0, "kind": "force", "kg": 5, "direction": "pull"}]
    ranked = api.orientations({"material": "pla", "goal": "strength"}, loads=loads)["orientations"]
    best = ranked[0]
    assert abs(best["up"][2]) == pytest.approx(1)
    strongest = max(ranked, key=lambda option: option["safety_factor"])
    assert abs(strongest["up"][2]) < 0.1
    assert strongest["safety_factor"] > best["safety_factor"]
    assert "tipping_risk" in {finding["rule"] for finding in api.check(strongest["up"])["findings"]}
    opts = api.orientations({"material": "pla"}, loads=loads)["orientations"]
    weaker = [o for o in opts if abs(o["up"][2]) > 0.9]
    assert weaker and any("holds" in r and "more" in r for r in weaker[0]["reasons"])
    assert any(abs(o["up"][2]) < 0.1 for o in opts)  # the strongest direction is always offered


def test_first_layer_spots_and_strips():
    from shapely.geometry import box

    from fdmcore.profile import Profile
    from fdmcore.refine import first_layer_stats

    p = Profile.from_dict({"material": "pla"})
    plate_and_tips = box(0, 0, 40, 40).union(box(50, 0, 53, 3)).union(box(50, 10, 52, 12))
    assert first_layer_stats(plate_and_tips, p) == {"small_spots": 2, "thin_share": pytest.approx(0.0, abs=0.01)}
    ring = box(0, 0, 40, 40).difference(box(1, 1, 39, 39))  # a thin-walled box standing open side down
    assert first_layer_stats(ring, p)["thin_share"] > 0.9


def test_standing_on_small_feet_is_explained():
    from shapely.geometry import box

    from fdmcore.additions import extrude

    legs = [extrude(box(x, y, x + 3, y + 3), 0, 10) for x in (0, 37) for y in (0, 37)]
    api.load(to_binary_stl(Mesh(np.concatenate([extrude(box(0, 0, 40, 40), 10, 13), *legs]))), "table")
    r = api.orientations({"material": "pla"})
    assert any("4 small spots on the bed" in t for t in r["as_loaded"]["reasons"])
    assert r["orientations"][0]["up"] == pytest.approx([0, 0, -1])  # top down: one solid patch on the bed


def test_warping_materials_trade_more_height_for_contact():
    """ABS gives the broad base more weight. PLA's smaller contact bonus still
    favours the lower, faster side pose when neither option needs support.
    """
    api.load(sample("l_bracket"), "l_bracket")
    pick = lambda material: api.orientations({"printer": "bambu_p1s", "material": material})["orientations"][0]["up"]
    assert pick("abs") == pytest.approx([0, 0, 1])
    assert abs(pick("pla")[1]) == pytest.approx(1)


def test_tall_thin_part_turned_along_a_moving_bed():
    """On a bed slinger (A1, MK4S: the bed moves along Y) a tall thin plate standing broadside to that motion
    wobbles; it is turned so its long side runs along Y (mistake #30). Printers with a still bed, and short or
    thick parts, are left as they are; the check and the 3MF use the same turn."""
    import io
    import zipfile

    import numpy as np

    from fdmcore import api
    from fdmcore.mesh import Mesh, to_binary_stl
    from test_formats import box

    api.load(to_binary_stl(Mesh(box([0, 0, 0], 1) * [60, 6, 100])), "plate")
    slinger = api.orientation([0, 0, 1], {"printer": "bambu_a1"})
    assert np.array(slinger["rotation"]) @ [1, 0, 0] == pytest.approx([0, 1, 0], abs=1e-6)
    assert any("as the bed moves" in r for r in slinger["reasons"])
    still = api.orientation([0, 0, 1], {"printer": "bambu_p1s"})
    assert np.array(still["rotation"]) @ [1, 0, 0] == pytest.approx([1, 0, 0], abs=1e-6)
    xml = zipfile.ZipFile(io.BytesIO(api.export([0, 0, 1], {"printer": "bambu_a1"}, {}))).read("3D/3dmodel.model").decode()
    ys = [float(v.split('y="')[1].split('"')[0]) for v in xml.split("<vertex ")[1:]]
    assert max(ys) - min(ys) == pytest.approx(60, abs=0.01)  # long side along Y in the file too
    api.load(to_binary_stl(Mesh(box([0, 0, 0], 1) * [60, 6, 30])), "short")
    assert np.array(api.orientation([0, 0, 1], {"printer": "bambu_a1"})["rotation"]) @ [1, 0, 0] == pytest.approx([1, 0, 0], abs=1e-6)


def test_copies_on_one_plate():
    """How many fit in rows 10 mm apart inside a 5 mm edge, set out round the bed's centre without overlapping;
    the 3MF carries them as build items of the one object (tools/slicer_check.py: all three slicers print them),
    PrusaSlicer's config counts the instances; the notes explain all at once vs one after another."""
    import io
    import zipfile

    from fdmcore.placement import plate_layout
    from test_formats import box

    most, spots = plate_layout((20, 20), (256, 256, 256))
    assert most == 64 and len(spots) == 64  # (246 + 10) // 30 = 8 a row
    xs, ys = np.array(spots).T
    assert xs.max() + 10 <= 128 - 5 and ys.min() - 10 >= -128 + 5
    assert min(np.hypot(*(np.array(a) - b)) for i, a in enumerate(spots) for b in spots[:i]) >= 30
    assert plate_layout((20, 20), (256, 256, 256), 3)[1] == [[-30.0, 0.0], [0.0, 0.0], [30.0, 0.0]]
    assert plate_layout((250, 250), (256, 256, 256)) == (1, [[0.0, 0.0]])  # fits alone, inside the edge margin or not
    assert plate_layout((300, 20), (256, 256, 256)) == (0, [])

    api.load(to_binary_stl(Mesh(box([0, 0, 0], 1) * [60, 30, 10])), "block")
    plan = api.plate([0, 0, 1], {}, 4)
    assert (plan["most"], plan["copies"], len(plan["offsets"])) == (18, 4, 4) and len(plan["notes"]) == 2
    assert "one after another" in plan["notes"][1].lower() and api.plate([0, 0, 1], {}, 1)["notes"] == []
    for slicer in ("orca", "prusa"):
        z = zipfile.ZipFile(io.BytesIO(api.export([0, 0, 1], {}, {}, slicer=slicer, copies=4)))
        assert z.read("3D/3dmodel.model").decode().count("<item ") == 4
        assert 'instances_count="4"' in z.read("Metadata/Slic3r_PE_model.config").decode()
    one = zipfile.ZipFile(io.BytesIO(api.export([0, 0, 1], {}, {}))).read("3D/3dmodel.model").decode()
    assert one.count("<item ") == 1 and "transform" not in one


def test_kept_direction_stays_an_option():
    """After a design change the part stays the way the user had it on the bed (T-353): the kept
    direction is always among the options, as the same option when it already was one."""
    api.load((SAMPLES / "l_bracket.stl").read_bytes(), "l_bracket")
    first = api.orientations()
    assert first["kept"] is None
    second = first["orientations"][1]["up"]
    again = api.orientations(keep=second)
    assert np.dot(again["orientations"][again["kept"]]["up"], second) > 0.999
    odd = list(np.array([1.0, 1.0, 0.3]) / np.linalg.norm([1.0, 1.0, 0.3]))  # no generic search picks this
    r = api.orientations(keep=odd)
    assert np.dot(r["orientations"][r["kept"]]["up"], odd) > 0.999


def assert_not_pareto_dominated(options):
    best = options[0]
    pick = (best["support_g"], -best["bed_area_mm2"], best["height_mm"])
    for option in options[1:]:
        alternative = (option["support_g"], -option["bed_area_mm2"], option["height_mm"])
        assert not (all(left <= right for left, right in zip(alternative, pick))
                    and any(left < right for left, right in zip(alternative, pick))), (best, option)


@pytest.mark.parametrize("path", sorted(SAMPLES.glob("*.stl")), ids=lambda path: path.stem)
def test_sample_pick_is_not_dominated_or_avoidably_unstable(path):
    api.load(path.read_bytes(), path.stem)
    options = api.orientations({"printer": "bambu_p1s", "material": "pla"})["orientations"]
    assert_not_pareto_dominated(options)
    bed_rules = {"tipping_risk", "small_bed_contact"}
    risks = lambda option: bed_rules & {finding["rule"] for finding in api.check(option["up"])["findings"]}
    if risks(options[0]):
        assert all(risks(option) for option in options[1:])


@pytest.mark.parametrize("goal", ["balanced", "speed", "material"])
def test_feature_test_plate_is_recommended_flat(goal):
    api.load(sample("feature_test"), "feature_test")
    options = api.orientations({"printer": "bambu_p1s", "material": "pla", "goal": goal})["orientations"]
    best = options[0]
    assert best["up"] == pytest.approx([0, 0, 1])
    assert best["bed_area_mm2"] > 5900
    assert best["height_mm"] == pytest.approx(40)
    assert_not_pareto_dominated(options)


def test_finalist_support_and_time_use_displayed_measurements():
    from fdmcore.checks import _estimate
    from fdmcore.orientation import _size

    api.load(sample("feature_test"), "feature_test")
    profile = api.Profile.from_dict(None)
    result = api.orientations()
    options = [*result["orientations"], result["as_loaded"]]
    support_scale = _size(api._session.mesh)**3 * api.SUPPORT_FILL / 1000 * profile.material["density_g_cm3"]
    time_ratios = []
    for option in options:
        terms = option["score_terms"]
        assert terms["support"] == pytest.approx(option["support_g"] / support_scale, abs=0.001)
        layers = api.slice_mesh(api.orient(api._session.mesh, np.array(option["rotation"])), profile.layer_height)
        minutes = _estimate(layers, profile)["print_time_min"]
        time_ratios.append(terms["time"] / minutes)
        assert sum(terms.values()) == pytest.approx(option["score"], abs=0.01)
    assert time_ratios == pytest.approx([time_ratios[0]] * len(options), rel=0.005)


@pytest.mark.parametrize("rule", ["tipping_risk", "small_bed_contact"])
def test_bed_checks_override_a_better_score(monkeypatch, rule):
    import importlib
    from fdmcore import checks

    api_orientation = importlib.import_module("fdmcore.api.orientation")

    bed_findings = checks._bed_findings
    refine = api_orientation._refine

    def force_score(mesh, results, placed, settings, profile, **kw):
        refine(mesh, results, placed, settings, profile, **kw)
        for result in results:
            if np.allclose(result.up, [0, 0, 1]):
                result.penalize("faces", 100)

    def bed_risk(layers, profile, thresholds):
        found = bed_findings(layers, profile, thresholds)
        return [{**finding, "rule": rule} if finding["rule"] == "tipping_risk" else finding for finding in found]

    monkeypatch.setattr(api_orientation, "_refine", force_score)
    monkeypatch.setattr(checks, "_bed_findings", bed_risk)
    api.load(sample("feature_test"), "feature_test")
    best, edge = api.orientations()["orientations"]
    assert best["up"] == pytest.approx([0, 0, 1])
    assert best["score"] > edge["score"]


def test_pareto_guard_overrides_other_score_terms(monkeypatch):
    import importlib
    from fdmcore import checks

    api_orientation = importlib.import_module("fdmcore.api.orientation")

    refine = api_orientation._refine

    def force_score(mesh, results, placed, settings, profile, **kw):
        refine(mesh, results, placed, settings, profile, **kw)
        for result in results:
            if np.allclose(result.up, [0, 0, 1]):
                result.penalize("faces", 100)

    api.load(sample("feature_test"), "feature_test")
    monkeypatch.setattr(api_orientation, "_refine", force_score)
    monkeypatch.setattr(checks, "_bed_findings", lambda layers, profile, thresholds: [])
    options = api.orientations()["orientations"]
    assert_not_pareto_dominated(options)
    assert options[0]["up"] == pytest.approx([0, 0, 1])
    assert options[0]["score"] > options[1]["score"]
    assert options[0]["score_terms"]["bed"] < 0


def test_only_finalists_get_bed_and_time_checks(monkeypatch):
    from fdmcore import checks

    sliced = []
    slice_mesh = checks.slice_mesh

    def record_slice(mesh, layer_height, max_layers):
        sliced.append(mesh)
        return slice_mesh(mesh, layer_height, max_layers)

    monkeypatch.setattr(checks, "slice_mesh", record_slice)
    api.load(sample("l_bracket"), "l_bracket")
    api.orientations(top=1)
    assert 2 <= len(sliced) <= 4


def test_bumpy_blob_pick_is_not_dominated():
    """Regenerate the audit's smooth 80k-face shape without a large binary fixture.

    There is no flat face: every option can still need adhesion help. The ranking
    must remain finite and Pareto-consistent even when no safe finalist exists.
    """
    longitude = np.linspace(0, 2 * np.pi, 200, endpoint=False)
    latitude = np.linspace(0, np.pi, 202)
    azimuth, polar = np.meshgrid(longitude, latitude)
    radius = 30 * (1 + 0.15 * np.sin(7 * azimuth) * np.sin(5 * polar)
                   + 0.05 * np.sin(23 * azimuth + 11 * polar))
    vertices = radius[..., None] * np.stack([np.sin(polar) * np.cos(azimuth),
                                            np.sin(polar) * np.sin(azimuth), np.cos(polar)], axis=-1)
    vertices[0], vertices[-1] = [0, 0, 30], [0, 0, -30]
    vertices[..., 2] *= 80 / np.ptp(vertices[..., 2])
    row, column = np.meshgrid(np.arange(201), np.arange(200), indexing="ij")
    lower = row * 200 + column
    beside = row * 200 + (column + 1) % 200
    faces = np.concatenate([np.stack([lower, lower + 200, beside], axis=-1)[1:].reshape(-1, 3),
                            np.stack([beside, lower + 200, beside + 200], axis=-1)[:-1].reshape(-1, 3)])
    mesh = Mesh(vertices.reshape(-1, 3)[faces])
    assert mesh.face_count == 80_000
    api.load(to_binary_stl(mesh), "bumpy_blob")
    options = api.orientations()["orientations"]
    assert_not_pareto_dominated(options)
    assert all(np.isfinite(option["score"]) for option in options)


def reason_option(**changes):
    """Finalists with identical card values unless a test changes one."""
    from fdmcore.orientation import OrientationResult

    values = dict(up=np.array([0, 0, 1]), score=0.0, support_area=0.0, support_volume=0.0,
                  bed_area=100.0, height=20.0, support_grams=3.0, print_time_min=30.0)
    values.update(changes)
    return OrientationResult(**values)


@pytest.mark.parametrize("changes, phrase", [
    ({"support_grams": 4.0}, "needs more support"),
    ({"bed_area": 80.0}, "has less contact with the bed"),
    ({"height": 23.0}, "prints taller"),
    ({"print_time_min": 34.0}, "takes longer to print"),
])
def test_lower_reasons_use_card_values_even_without_score_gaps(changes, phrase):
    assert api.why_lower(reason_option(**changes), reason_option()) == "Ranked lower: " + phrase


@pytest.mark.parametrize("changes", [
    {"support_grams": 2.0, "terms": {"support": 100.0}},  # the reported 2 g vs 3 g bug
    {"support_grams": 3.49, "terms": {"support": 100.0}},  # both cards show 3 g
    {"support_grams": 3.5},  # exactly the 0.5 g floor
    {"bed_area": 90.0, "height": 22.0, "print_time_min": 33.0},  # exactly 10%: a near tie
    {"bed_area": 99.6, "height": 20.04, "print_time_min": 30.4},  # round to the same card values
    {"terms": {"support": 9.0, "height": 9.0, "bed": 9.0, "time": 9.0}},
    {"terms": {"strength": 0.04}},
])
def test_lower_reason_near_tie_never_invents_a_numeric_cause(changes):
    assert api.why_lower(reason_option(**changes), reason_option()) == "About as good: pick whichever suits you"


@pytest.mark.parametrize("option_g, best_g", [(0.9, 0.0), (0.9, 0.1), (0.99, 1.01), (10.6, 10.0)])
def test_lower_support_reason_respects_small_weight_labels_and_relative_floor(option_g, best_g):
    assert api.why_lower(reason_option(support_grams=option_g), reason_option(support_grams=best_g)).startswith("About as good")


@pytest.mark.parametrize("rule, phrase", [
    ("tipping_risk", "may tip over"),
    ("small_bed_contact", "has too little contact to stay on the bed"),
])
def test_lower_reason_uses_own_bed_finding_when_numbers_are_tied(rule, phrase):
    option = reason_option(bed_rules=(rule,))
    assert api.why_lower(option, reason_option()) == "Ranked lower: " + phrase
    assert api.why_lower(option, reason_option(bed_rules=(rule,))).startswith("About as good")


@pytest.mark.parametrize("term, phrase", [
    ("stability", "may tip or wobble"),
    ("first_layer", "has small spots or thin strips on the first layer"),
    ("weak", "has more weak spots along the layers"),
    ("strength", "is weaker under your load"),
])
def test_lower_reason_explains_non_numeric_penalties(term, phrase):
    assert api.why_lower(reason_option(terms={term: 0.5}), reason_option()) == "Ranked lower: " + phrase


@pytest.mark.parametrize("kind, phrase", [
    ("clean", "puts support marks on a face you want clean"),
    ("bed", "leaves a face you marked off the bed"),
    ("vertical", "tilts a face you want upright"),
])
def test_lower_reason_explains_each_marked_face_preference(kind, phrase):
    option = reason_option(terms={"faces": 1.0}, face_penalties={kind: 1.0})
    assert api.why_lower(option, reason_option()) == "Ranked lower: " + phrase


def test_lower_reason_explains_fit_guard_without_a_score_term():
    assert api.why_lower(reason_option(), reason_option(), fits=False) == "Ranked lower: doesn't fit the printer"
    assert api.why_lower(reason_option(), reason_option(), fits=False, best_fits=False).startswith("About as good")


@pytest.mark.parametrize("path", sorted(SAMPLES.glob("*.stl")) + sorted(
    p for p in (Path(__file__).parent / "corpus").glob("*.stl") if not p.stem.endswith("_80k")),
                         ids=lambda path: path.stem)
def test_lower_sentences_agree_with_sample_and_corpus_cards(path):
    api.load(path.read_bytes(), path.stem)
    result = api.orientations({"printer": "bambu_p1s", "material": "pla"})
    best = result["orientations"][0]

    def shown_time(option):
        return float(next(reason.split()[1] for reason in option["reasons"] if reason.endswith("min to print")))

    for option in [*result["orientations"][1:], result["as_loaded"]]:
        sentence = option["why_lower"]
        context = (path.stem, sentence, option, best)
        if "needs more support" in sentence:
            assert option["support_g"] > best["support_g"], context
            assert option["support_summary"] != best["support_summary"], context
        if "taller" in sentence:
            assert option["height_mm"] > best["height_mm"], context
            assert float(f"{option['height_mm']:.1f}") > float(f"{best['height_mm']:.1f}"), context
        if "has less contact" in sentence:
            assert option["bed_area_mm2"] < best["bed_area_mm2"], context
            assert round(option["bed_area_mm2"]) < round(best["bed_area_mm2"]), context
        if "takes longer" in sentence:
            assert shown_time(option) > shown_time(best), context
    for option in [*result["orientations"], result["as_loaded"]]:
        assert f"Flat contact with the bed: {option['bed_area_mm2']:.0f} mm²" in option["reasons"]
