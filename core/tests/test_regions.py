import json
from pathlib import Path

import numpy as np
import pytest
from shapely.geometry import Point, Polygon, box

from fdmcore import api
from fdmcore.additions import extrude
from fdmcore.mesh import Mesh, load_stl, to_binary_stl
from fdmcore.regions import EDGE_BAND_RADIUS, edge_load, find_regions, region_info, sample_polyline, unpack_polylines

ROOT = Path(__file__).resolve().parents[2]
SAMPLES = ROOT / "web" / "samples"
CORPUS = Path(__file__).parent / "corpus"


def walls(poly, height=10.0):
    """Side-wall regions of an extruded outline: (count, biggest area)."""
    m = Mesh(extrude(poly, 0.0, height))
    info = [x for x in region_info(m, find_regions(m)) if abs(x["normal"][2]) < 0.5]
    return len(info), max(x["area_mm2"] for x in info)


def test_rounded_outline_splits_into_sides_and_corners():
    # A dog-bone with 2 mm rounds on all 12 corners: each flat side is two triangles, as small as
    # a fillet strip, and used to join the fillets into one wall all round the part.
    p = Polygon([(0, 0), (40, 0), (40, 12), (26, 16), (26, 24), (40, 28), (40, 40), (0, 40), (0, 28), (14, 24), (14, 16), (0, 12)])
    p = p.buffer(-2, quad_segs=8).buffer(2, quad_segs=8).buffer(2, quad_segs=8).buffer(-2, quad_segs=8)
    count, biggest = walls(p)
    assert count == 24  # 12 flat sides + 12 round corners
    assert biggest < 0.3 * p.exterior.length * 10


def test_hole_wall_stays_one_face():
    plate = Polygon([(0, 0), (30, 0), (30, 30), (0, 30)]).difference(Point(15, 15).buffer(4, quad_segs=16))
    m = Mesh(extrude(plate, 0.0, 5.0))
    region = find_regions(m)
    info = region_info(m, region)
    hole = [x for x in info if abs(x["normal"][2]) < 0.5 and x["kind"] == "curved"]
    assert len(hole) == 1
    assert hole[0]["area_mm2"] > 0.95 * np.pi * 8 * 5  # the whole round wall, not a few strips


def _assert_packed_edges_valid(packed: dict, n_regions: int):
    """Check compact binary layout invariants for packed polylines."""
    assert isinstance(packed["points"], bytes)
    assert isinstance(packed["offsets"], bytes)
    offsets = np.frombuffer(packed["offsets"], dtype="<u4")
    meta = packed["meta"]
    assert len(offsets) == len(meta) + 1
    assert int(offsets[0]) == 0
    assert int(offsets[-1]) * 12 == len(packed["points"])
    lines = unpack_polylines(packed)
    assert len(lines) == len(meta)
    for i, (m, pts) in enumerate(zip(meta, lines)):
        assert m["id"] == i
        assert len(pts) >= 2
        assert m["length_mm"] > 0
        assert len(m["midpoint"]) == 3 and len(m["normal"]) == 3
        assert len(m["regions"]) == 2
        assert all(0 <= r < n_regions for r in m["regions"])
        if m["closed"]:
            assert np.linalg.norm(pts[-1] - pts[0]) < 1e-3
    # Non-binary fields must be JSON-serialisable.
    json.dumps(meta)
    return lines


def test_api_regions_l_bracket_stl_kinds_and_edges():
    """One api.regions() call returns face_regions, flat/hole regions with diameters, sharp
    feature_edges (including closed hole rims) and the 30 mm concave inside corner."""
    data = (SAMPLES / "l_bracket.stl").read_bytes()
    info = api.load(data, "l_bracket.stl")
    r = api.dispatch("regions")

    face_regions = np.frombuffer(r["face_regions"], dtype="<u4")
    assert len(face_regions) == info["faces"]
    regs = r["regions"]
    assert int(face_regions.max()) == len(regs) - 1
    assert r["edge_radius_mm"] == pytest.approx(EDGE_BAND_RADIUS)

    holes = [x for x in regs if x["kind"] == "hole"]
    flats = [x for x in regs if x["kind"] == "flat"]
    assert len(flats) == 8
    assert sorted(round(x["hole_diameter"], 1) for x in holes) == [6.0, 8.0]
    assert all("hole_diameter" not in x for x in regs if x["kind"] != "hole")

    feat_lines = _assert_packed_edges_valid(r["feature_edges"], len(regs))
    conc_lines = _assert_packed_edges_valid(r["concave_edges"], len(regs))
    # L-bracket has exactly one inside corner: 30 mm straight crease simplified to 2 endpoints.
    assert len(conc_lines) == 1
    assert len(conc_lines[0]) == 2
    assert r["concave_edges"]["meta"][0]["length_mm"] == pytest.approx(30.0, abs=0.1)
    assert not r["concave_edges"]["meta"][0]["closed"]
    # Feature edges include the 4 closed circular hole rims (two Ø6 rims + two Ø8 rims).
    closed_feats = [m for m in r["feature_edges"]["meta"] if m["closed"]]
    assert len(closed_feats) == 4
    rim_lens = sorted(m["length_mm"] for m in closed_feats)
    assert rim_lens == pytest.approx([np.pi * 6, np.pi * 6, np.pi * 8, np.pi * 8], rel=0.03)
    assert len(feat_lines) >= 16


def test_api_regions_classifies_flat_cylinder_hole_and_curved():
    """Verify all four region kinds: flat, cylinder (boss/post), hole (with diameter) and curved (fillet)."""
    api.load((SAMPLES / "enclosure.stl").read_bytes(), "enclosure.stl")
    r = api.regions()
    kinds = {x["kind"] for x in r["regions"]}
    assert {"flat", "cylinder", "hole"} <= kinds
    cyls = [x for x in r["regions"] if x["kind"] == "cylinder"]
    holes = [x for x in r["regions"] if x["kind"] == "hole"]
    assert len(cyls) == 4 and len(holes) == 4
    assert all(h["hole_diameter"] == pytest.approx(2.8, abs=0.1) for h in holes)
    assert all("hole_diameter" not in c for c in cyls)

    # Rounded corner fillets (90 deg arcs) stay "curved", never misclassified as hole or cylinder.
    plate = box(0, 0, 30, 30).buffer(-3, quad_segs=8).buffer(3, quad_segs=8)
    api.load(to_binary_stl(Mesh(extrude(plate, 0.0, 6.0))), "rounded_plate.stl")
    r2 = api.regions()
    assert {x["kind"] for x in r2["regions"]} == {"flat", "curved"}
    assert all("hole_diameter" not in x for x in r2["regions"])
    _assert_packed_edges_valid(r2["feature_edges"], len(r2["regions"]))
    assert len(r2["concave_edges"]["meta"]) == 0


def test_api_regions_step_face_ids_and_split_cylinders():
    """STEP imports pass exact CAD face_ids (including OpenCascade's 180 deg split half-cylinders).
    Verify regions preserve those CAD faces, classify split holes/cylinders accurately, and never
    emit smooth cylinder seam lines as feature edges."""
    mesh = load_stl((SAMPLES / "l_bracket.stl").read_bytes())
    base_regions = find_regions(mesh)
    base_info = region_info(mesh, base_regions)
    # Simulate an OpenCascade STEP export where each cylindrical hole is split into two 180 deg B-Rep faces.
    step_ids = base_regions.astype(int).copy()
    next_id = int(step_ids.max()) + 1
    centroids = mesh.triangles.mean(axis=1)
    for reg in base_info:
        if reg["kind"] == "curved":
            sel = np.flatnonzero(base_regions == reg["id"])
            # Split the hole wall in half along Y = 0 (both holes sit on Y = 0 in l_bracket).
            half = sel[centroids[sel, 1] >= reg["centroid"][1]]
            step_ids[half] = next_id
            next_id += 1

    api.load(to_binary_stl(mesh), "l_bracket.step", face_ids=step_ids.tolist())
    r = api.regions()
    face_regions = np.frombuffer(r["face_regions"], dtype="<u4")
    assert len(r["regions"]) == next_id  # exact STEP face count preserved (8 flat + 4 half-hole faces)
    assert np.array_equal(face_regions, step_ids)

    holes = [x for x in r["regions"] if x["kind"] == "hole"]
    assert len(holes) == 4  # both halves of Ø6 and both halves of Ø8 recognised as holes
    assert sorted(round(x["hole_diameter"], 1) for x in holes) == [6.0, 6.0, 8.0, 8.0]

    # Inside corner is still the single 30 mm crease; smooth half-cylinder seams are NOT feature edges.
    conc_lines = _assert_packed_edges_valid(r["concave_edges"], len(r["regions"]))
    feat_lines = _assert_packed_edges_valid(r["feature_edges"], len(r["regions"]))
    assert len(conc_lines) == 1
    assert r["concave_edges"]["meta"][0]["length_mm"] == pytest.approx(30.0, abs=0.1)
    # Still exactly 4 closed circular rims (no vertical seam split across the smooth cylinder halves).
    assert sum(1 for m in r["feature_edges"]["meta"] if m["closed"]) == 4
    assert len(feat_lines) >= 16

    # Scaling invalidates cached edges and scales edge lengths/midpoints proportionally.
    api.scale(2.0)
    r_scaled = api.regions()
    assert r_scaled["concave_edges"]["meta"][0]["length_mm"] == pytest.approx(60.0, abs=0.2)

    # Also verify a STEP-split cylindrical boss (tall_pin) classifies both halves as "cylinder".
    pin = load_stl((CORPUS / "tall_pin.stl").read_bytes())
    pin_regs = find_regions(pin)
    pin_info = region_info(pin, pin_regs)
    pin_step = pin_regs.astype(int).copy()
    pid = int(pin_step.max()) + 1
    pin_c = pin.triangles.mean(axis=1)
    for reg in pin_info:
        if reg["kind"] == "curved":
            sel = np.flatnonzero(pin_regs == reg["id"])
            pin_step[sel[pin_c[sel, 0] >= reg["centroid"][0]]] = pid
            pid += 1
    api.load(to_binary_stl(pin), "tall_pin.step", face_ids=pin_step.tolist())
    rp = api.regions()
    cyls = [x for x in rp["regions"] if x["kind"] == "cylinder"]
    assert len(cyls) == 4  # 2 cylinders x 2 halves each
    assert len(rp["concave_edges"]["meta"]) == 1  # circular inside corner at pin base
    assert rp["concave_edges"]["meta"][0]["closed"]


def test_sample_polyline_and_edge_load_band():
    """Polylines resample continuously along arc length so strength.py bands have no gaps."""
    line = np.array([[0.0, 0.0, 0.0], [30.0, 0.0, 0.0]])
    load = edge_load(line, kind="fixed", radius=2.0)
    pts = np.asarray(load["points"])
    assert load["radius"] == 2.0 and load["kind"] == "fixed"
    assert len(pts) >= 21
    step = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    assert step.max() <= 1.5 + 1e-6
    assert np.allclose(pts[0], [0, 0, 0]) and np.allclose(pts[-1], [30, 0, 0])

    # Closed loop doesn't duplicate the seam point.
    ang = np.linspace(0, 2 * np.pi, 65)
    circle = np.column_stack([5 * np.cos(ang), 5 * np.sin(ang), np.zeros_like(ang)])
    sampled = sample_polyline(circle, spacing=1.5)
    assert np.linalg.norm(sampled[-1] - sampled[0]) > 0.5


def test_held_feature_edge_constrains_strength_solve():
    """Holding a feature edge via points + radius band (or via edge id) actually constrains the
    FEA solve: displacement drops sharply and safety rises vs leaving that edge free."""
    api.load(to_binary_stl(Mesh(extrude(box(0, 0, 60, 15), 0.0, 6.0))), "beam.stl")
    r = api.regions()
    regs = r["regions"]
    left_face = min((x for x in regs if x["kind"] == "flat"), key=lambda x: x["centroid"][0])["id"]
    mid_push = {"point": [30.0, 7.5, 6.0], "radius": 3.0, "kind": "force", "kg": 5.0, "direction": "down"}

    # Pick the bottom feature edge at the far end (x = 60, z = 0).
    feat_lines = unpack_polylines(r["feature_edges"])
    far_idx = max(
        range(len(feat_lines)),
        key=lambda i: r["feature_edges"]["meta"][i]["midpoint"][0] - 2.0 * r["feature_edges"]["meta"][i]["midpoint"][2],
    )
    far_meta = r["feature_edges"]["meta"][far_idx]
    assert far_meta["midpoint"][0] == pytest.approx(60.0, abs=0.1)
    assert far_meta["midpoint"][2] == pytest.approx(0.0, abs=0.1)
    assert far_meta["length_mm"] == pytest.approx(15.0, abs=0.1)

    free = api.strength([0, 0, 1], [{"region": left_face, "kind": "fixed"}, mid_push])
    held_band = edge_load(feat_lines[far_idx], kind="fixed", radius=2.0)
    constrained = api.strength([0, 0, 1], [{"region": left_face, "kind": "fixed"}, held_band, mid_push])
    via_edge_id = api.strength(
        [0, 0, 1],
        [{"region": left_face, "kind": "fixed"}, {"edge": far_idx, "edge_kind": "feature", "kind": "fixed", "radius": 2.0}, mid_push],
    )

    assert constrained["solver_converged"] and free["solver_converged"]
    assert constrained["max_displacement_mm"] < 0.5 * free["max_displacement_mm"]
    assert constrained["safety_factor"] > 1.5 * free["safety_factor"]
    assert via_edge_id["max_displacement_mm"] == pytest.approx(constrained["max_displacement_mm"], rel=1e-4)
    assert via_edge_id["safety_factor"] == pytest.approx(constrained["safety_factor"], rel=1e-4)


def test_held_concave_edge_and_loaded_edge_on_l_bracket():
    """Holding the concave inside corner constrains l_bracket.stl (smaller displacement than free),
    and a solve with ONLY a held inside corner + a loaded outer edge converges cleanly."""
    api.load((SAMPLES / "l_bracket.stl").read_bytes(), "l_bracket.stl")
    r = api.regions()
    regs = r["regions"]
    back_face = min((x for x in regs if x["kind"] == "flat"), key=lambda x: x["centroid"][0])["id"]
    conc_lines = unpack_polylines(r["concave_edges"])
    feat_lines = unpack_polylines(r["feature_edges"])
    assert len(conc_lines) == 1

    # Far outer top edge of the horizontal foot (x = +30, z = 5).
    tip_idx = max(
        range(len(feat_lines)),
        key=lambda i: r["feature_edges"]["meta"][i]["midpoint"][0] + 0.5 * r["feature_edges"]["meta"][i]["midpoint"][2],
    )
    tip_load = edge_load(feat_lines[tip_idx], kind="force", radius=2.0, kg=5.0, direction=[0.0, 0.0, -1.0])
    corner_hold = edge_load(conc_lines[0], kind="fixed", radius=2.0)

    free = api.strength([0, 0, 1], [{"region": back_face, "kind": "fixed"}, tip_load])
    held_corner = api.strength([0, 0, 1], [{"region": back_face, "kind": "fixed"}, corner_hold, tip_load])
    assert held_corner["solver_converged"]
    assert held_corner["max_displacement_mm"] < 0.85 * free["max_displacement_mm"]

    # Holding ONLY the inside corner + loading an outer feature edge also solves cleanly.
    corner_only = api.strength([0, 0, 1], [corner_hold, tip_load])
    assert corner_only["solver_converged"]
    assert np.isfinite(corner_only["safety_factor"]) and corner_only["safety_factor"] > 0
    assert corner_only["max_displacement_mm"] > 0


def test_regions_large_organic_mesh_compact_and_valid():
    """20k-triangle organic mesh produces valid packed edge buffers without hole misclassifications."""
    api.load((CORPUS / "bumpy_blob_20k.stl").read_bytes(), "bumpy_blob_20k.stl")
    r = api.regions()
    assert all("hole_diameter" not in x for x in r["regions"])
    _assert_packed_edges_valid(r["feature_edges"], len(r["regions"]))
    _assert_packed_edges_valid(r["concave_edges"], len(r["regions"]))


def test_noisy_mesh_edges_are_capped():
    """A bumpy organic mesh has thousands of tiny sharp edges; only the longest, pickable ones are sent."""
    from fdmcore import regions as rg

    rng = np.random.default_rng(0)
    phi, theta = np.meshgrid(np.linspace(0, np.pi, 60), np.linspace(0, 2 * np.pi, 120), indexing="ij")
    r = 20 + rng.normal(0, 1.5, phi.shape)
    r[:, -1] = r[:, 0]
    r[0, :], r[-1, :] = r[0, 0], r[-1, 0]
    xyz = np.stack([r * np.sin(phi) * np.cos(theta), r * np.sin(phi) * np.sin(theta), r * np.cos(phi)], axis=-1)
    q = np.stack([xyz[:-1, :-1], xyz[1:, :-1], xyz[1:, 1:], xyz[:-1, 1:]], axis=2).reshape(-1, 4, 3)
    mesh = Mesh(np.concatenate([q[:, [0, 1, 2]], q[:, [0, 2, 3]]]))
    for packed in rg.extract_edges(mesh, rg.find_regions(mesh)):
        assert len(packed["meta"]) <= rg.MAX_EDGES
        assert all(m["length_mm"] >= rg.MIN_EDGE_MM for m in packed["meta"])
