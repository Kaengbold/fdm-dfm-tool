"""Working shapes (gear teeth, splines, knurls, threads, snap hooks, sized fits) must not be
rounded or chamfered at the bed, while plain corners like a tray's still are.
"""

from pathlib import Path

import numpy as np
import pytest
import shapely
from shapely.geometry import Point, Polygon, box
from shapely.geometry.polygon import orient as orient_poly
from shapely.ops import unary_union

from fdmcore import api
from fdmcore.additions import extrude
from fdmcore.mesh import Mesh, to_binary_stl

CORPUS = Path(__file__).parent / "corpus"
UP = [0, 0, 1]


def _prism_mesh(poly, height: float) -> Mesh:
    """Closed prism of a 2D polygon (with or without holes) via constrained triangulation."""
    poly = orient_poly(poly.simplify(0), 1.0)
    caps = shapely.get_coordinates(shapely.get_parts(shapely.constrained_delaunay_triangles(poly))).reshape(-1, 4, 2)[:, :3]
    u, v = caps[:, 1] - caps[:, 0], caps[:, 2] - caps[:, 0]
    cross = u[:, 0] * v[:, 1] - u[:, 1] * v[:, 0]
    caps = np.where((cross < 0)[:, None, None], caps[:, ::-1], caps)
    lift = lambda t, z: np.concatenate([t, np.full((*t.shape[:2], 1), z)], axis=2)  # noqa: E731
    walls = []
    for ring in [poly.exterior, *poly.interiors]:
        pts = np.asarray(ring.coords)
        a, b = pts[:-1], pts[1:]
        walls += [
            np.stack([lift(a[:, None], 0)[:, 0], lift(b[:, None], 0)[:, 0], lift(b[:, None], height)[:, 0]], 1),
            np.stack([lift(a[:, None], 0)[:, 0], lift(b[:, None], height)[:, 0], lift(a[:, None], height)[:, 0]], 1),
        ]
    return Mesh(np.concatenate([lift(caps[:, ::-1], 0), lift(caps, height), *walls]))


def _edit_pts(edit: dict) -> np.ndarray:
    return np.frombuffer(edit["mesh"], "<f4").reshape(-1, 3).astype(float)


def _through_threaded_block(major=10.0, pitch=1.5, length=10.0, half_w=12.0, n_theta=72, per_pitch=16) -> Mesh:
    """A square block with an internal 60° thread running all the way through to z = 0."""
    depth = 0.6 * pitch
    th = np.linspace(0, 2 * np.pi, n_theta, endpoint=False)
    zs = np.linspace(0, length, int(length / pitch * per_pitch) + 1)
    T, Z = np.meshgrid(th, zs)
    s = ((Z - T / (2 * np.pi) * pitch) / pitch) % 1.0
    R = major / 2 - depth + depth * np.interp(s, [0, 0.15, 0.5, 0.65, 1.0], [0, 0, 1, 1, 0])
    P = np.stack([R * np.cos(T), R * np.sin(T), Z], axis=-1)
    nz, nt = P.shape[:2]
    i, j = np.meshgrid(np.arange(nz - 1), np.arange(nt), indexing="ij")
    a, b, c, d = P[i, j], P[i, (j + 1) % nt], P[i + 1, (j + 1) % nt], P[i + 1, j]
    # Thread wall facing inward into the hole (reversed from an external bolt).
    hole_wall = np.concatenate([np.stack([a, c, b], 2).reshape(-1, 3, 3), np.stack([a, d, c], 2).reshape(-1, 3, 3)])
    outer = box(-half_w, -half_w, half_w, half_w)
    # Top and bottom faces have the thread's start/end profile as their hole.
    bot_poly = orient_poly(outer.difference(Polygon(P[0, :, :2])), 1.0)
    top_poly = orient_poly(outer.difference(Polygon(P[-1, :, :2])), 1.0)

    def cap(poly, z, flip):
        tris = shapely.get_coordinates(shapely.get_parts(shapely.constrained_delaunay_triangles(poly))).reshape(-1, 4, 2)[:, :3]
        u, v = tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]
        tris = np.where((u[:, 0] * v[:, 1] - u[:, 1] * v[:, 0] < 0)[:, None, None], tris[:, ::-1], tris)
        out = np.concatenate([tris, np.full((*tris.shape[:2], 1), z)], axis=2)
        return out[:, ::-1] if flip else out

    ring = np.asarray(outer.exterior.coords)
    oa, ob = ring[:-1], ring[1:]
    lift = lambda p, z: np.column_stack([p, np.full(len(p), z)])  # noqa: E731
    side = np.concatenate([
        np.stack([lift(oa, 0), lift(ob, 0), lift(ob, length)], 1),
        np.stack([lift(oa, 0), lift(ob, length), lift(oa, length)], 1),
    ])
    return Mesh(np.concatenate([cap(bot_poly, 0.0, True), cap(top_poly, length, False), side, hole_wall]))


@pytest.mark.parametrize("material", ["pla", "abs"])
def test_gear_keeps_teeth_untouched_and_notes_it(material):
    api.load((CORPUS / "gear.stl").read_bytes(), "gear")
    edits = api.design(UP, {"material": material})["edits"]
    assert not [e for e in edits if e["rule"] == "sharp_bed_corners"]
    foot = next(e for e in edits if e["id"] == "foot-chamfer")
    # Only the Ø8 mm central bore is chamfered (radius ~4 mm); nothing near the teeth (radius 23–26 mm).
    pts = _edit_pts(foot)
    radii = np.hypot(pts[:, 0], pts[:, 1])
    assert radii.max() < 6.0
    assert foot["description"].count("Left the teeth alone.") == 1


@pytest.mark.parametrize("material", ["pla", "abs"])
def test_tray_still_gets_four_corners_rounded_and_bottom_chamfer(material):
    api.load((CORPUS / "tray.stl").read_bytes(), "tray")
    edits = api.design(UP, {"material": material})["edits"]
    corners = [e for e in edits if e["rule"] == "sharp_bed_corners"]
    assert len(corners) == 4
    assert any(e["id"] == "foot-chamfer" for e in edits)
    assert all("Left " not in e["description"] for e in edits)


def test_knurl_narrow_gear_internal_spline_sector_gear_and_plain_polygons():
    # 1. Triangular knurled cylinder (period 1: one tip corner per tooth).
    n_teeth = 24
    angles = np.linspace(0, 2 * np.pi, 2 * n_teeth, endpoint=False)
    radii = np.where(np.arange(2 * n_teeth) % 2 == 0, 22.0, 19.5)
    knurl = Polygon(np.column_stack([radii * np.cos(angles), radii * np.sin(angles)]))
    api.load(to_binary_stl(_prism_mesh(knurl, 8.0)), "knurl")
    edits = api.design(UP, {"material": "abs"})["edits"]
    assert not [e for e in edits if e["rule"] in ("sharp_bed_corners", "elephant_foot")]

    # 2. Narrow-tooth 12-tooth spur gear (period 2: uneven tip vs gap angular spacing).
    teeth = []
    for k in range(12):
        a = k * 2 * np.pi / 12
        c, s = np.cos(a), np.sin(a)
        rot = np.array([[c, -s], [s, c]])
        tooth = np.array([[18.0, -1.2], [23.0, -1.2], [23.0, 1.2], [18.0, 1.2]]) @ rot.T
        teeth.append(Polygon(tooth))
    narrow_gear = unary_union([Point(0, 0).buffer(19.0, 48), *teeth])
    api.load(to_binary_stl(_prism_mesh(narrow_gear, 8.0)), "narrow_gear")
    edits = api.design(UP, {"material": "abs"})["edits"]
    assert not [e for e in edits if e["rule"] in ("sharp_bed_corners", "elephant_foot")]

    # 3. Square plate with an 8-tooth internal spline: outer corners & edge kept, spline untouched.
    sp_ang = np.linspace(0, 2 * np.pi, 16, endpoint=False)
    sp_rad = np.where(np.arange(16) % 2 == 0, 9.0, 6.5)
    spline_hole = Polygon(np.column_stack([sp_rad * np.cos(sp_ang), sp_rad * np.sin(sp_ang)]))
    plate = box(-25, -25, 25, 25).difference(spline_hole)
    api.load(to_binary_stl(_prism_mesh(plate, 8.0)), "spline_plate")
    edits = api.design(UP, {"material": "abs"})["edits"]
    assert sum(e["rule"] == "sharp_bed_corners" for e in edits) == 4
    foot = next(e for e in edits if e["id"] == "foot-chamfer")
    pts = _edit_pts(foot)
    assert np.hypot(pts[:, 0], pts[:, 1]).min() > 20.0  # only the 50 mm square exterior, not the spline
    assert "Left the teeth alone." in foot["description"]

    # 4. Sector gear on a rectangular bracket: teeth skipped, straight bracket corners & edges kept.
    sec_teeth = []
    for k in range(8):
        a = np.radians(-63 + k * 18)
        c, s = np.cos(a), np.sin(a)
        rot = np.array([[c, -s], [s, c]])
        sec_teeth.append(Polygon(np.array([[22.0, -1.5], [27.0, -1.5], [27.0, 1.5], [22.0, 1.5]]) @ rot.T))
    sector = unary_union([Point(0, 0).buffer(23.0, 64).intersection(box(0, -30, 30, 30)), box(-25, -23, 0, 23), *sec_teeth])
    api.load(to_binary_stl(_prism_mesh(sector, 8.0)), "sector_gear")
    edits = api.design(UP, {"material": "abs"})["edits"]
    corners = [e for e in edits if e["rule"] == "sharp_bed_corners"]
    assert len(corners) == 2  # only the two back corners of the rectangular bracket
    assert all(_edit_pts(e)[:, 0].max() < 0 for e in corners)
    assert any("Left the teeth alone." in e["description"] for e in corners)
    foot = next(e for e in edits if e["id"] == "foot-chamfer")
    assert _edit_pts(foot)[:, 0].max() < 20.0  # stays away from the toothed rim (x ~ 22–27)
    assert "Left the teeth alone." in foot["description"]

    # 5. Plain regular hexagon and octagon are NOT teeth: bottom chamfer stays, no teeth note.
    for sides in (6, 8):
        a = np.linspace(0, 2 * np.pi, sides, endpoint=False)
        poly = Polygon(np.column_stack([20.0 * np.cos(a), 20.0 * np.sin(a)]))
        api.load(to_binary_stl(Mesh(extrude(poly, 0, 8.0))), f"poly_{sides}")
        edits = api.design(UP, {"material": "abs"})["edits"]
        foot = next(e for e in edits if e["id"] == "foot-chamfer")
        assert "Left " not in foot["description"]


def test_close_corners_and_snap_hook_skipped():
    """A plate with a snap-fit hook sticking off one side: hook corners (< 6 line widths apart)
    are skipped while the plate's four outer corners and straight edges keep their edits."""
    # Realistic cantilever snap hook: 2.0 mm tab, 2.0 mm barb with a 45° lead-in ramp at the tip
    # (all hook edges >= 2.0 mm so _sharp_corners sees them, and < 2.7 mm so close-corners skips them).
    hook = Polygon([(60, 18), (72, 18), (72, 20.0), (70.0, 22.0), (67.9, 22.0), (67.9, 20.0), (60, 20.0)])
    shape = unary_union([box(0, 0, 60, 40), hook])
    api.load(to_binary_stl(_prism_mesh(shape, 8.0)), "snap_plate")
    edits = api.design(UP, {"material": "abs"})["edits"]
    corners = [e for e in edits if e["rule"] == "sharp_bed_corners"]
    assert len(corners) == 4
    assert all(_edit_pts(e)[:, 0].max() < 62.0 for e in corners)  # none on the hook at x ~ 68–72
    assert any("Left the fine details alone." in e["description"] for e in corners)
    foot = next(e for e in edits if e["id"] == "foot-chamfer")
    assert _edit_pts(foot)[:, 0].max() < 62.0  # chamfer stays off the snap tab and hook
    assert "Left the fine details alone." in foot["description"]
    m3d = pytest.importorskip("manifold3d")
    assert m3d is not None
    from fdmcore.booleans import apply_edits

    assert apply_edits(api._require().mesh, [*corners, foot]).volume() > 0


def test_sized_fits_for_holes_posts_slots_and_gear_bore():
    # 1. Plate with a round hole sized for a sliding fit: hole excluded from foot chamfer.
    plate = Mesh(extrude(box(0, 0, 40, 30).difference(Point(12, 15).buffer(4, 64)), 0, 6))
    api.load(to_binary_stl(plate), "fit_plate")
    hole = next(f for f in api.design(UP, {})["features"] if f["type"] == "hole")
    before = next(e for e in api.design(UP, {})["edits"] if e["id"] == "foot-chamfer")
    assert len(before["pieces"]) == 2
    api.set_fit(hole["region"], UP, 8.0, "sliding")
    after = next(e for e in api.design(UP, {})["edits"] if e["id"] == "foot-chamfer")
    assert len(after["pieces"]) == 1
    pts = _edit_pts(after)
    assert np.hypot(pts[:, 0] - 12, pts[:, 1] - 15).min() > 6.0
    assert "Left the sized fit alone." in after["description"]

    # 2. Standalone round post with a sized fit: foot chamfer dropped completely.
    api.load(to_binary_stl(Mesh(extrude(Point(0, 0).buffer(5, 64), 0, 12))), "fit_peg")
    post = next(f for f in api.design(UP, {})["features"] if f["type"] == "boss")
    assert any(e["id"] == "foot-chamfer" for e in api.design(UP, {})["edits"])
    api.set_fit(post["region"], UP, 10.5, "sliding")
    assert not any(e["id"] == "foot-chamfer" for e in api.design(UP, {})["edits"])

    # 3. Block with a flat through-slot sized via set_gap: slot walls & mouth corners untouched.
    slot_poly = box(0, 0, 40, 30).difference(box(17, 0, 23, 12))
    api.load(to_binary_stl(_prism_mesh(slot_poly, 10.0)), "fit_slot")
    api.regions()
    s = api._require()
    normals, _ = s.mesh.normals_and_areas()
    centres = s.mesh.triangles.mean(axis=1)
    face = lambda n, x: int(s.regions[np.flatnonzero((normals @ n > 0.99) & (np.abs(centres[:, 0] - x) < 1e-3))[0]])  # noqa: E731
    left_wall, right_wall = face([1, 0, 0], 17.0), face([-1, 0, 0], 23.0)
    api.set_gap(left_wall, right_wall, UP, 6.0, "sliding")
    edits = api.design(UP, {"material": "abs"})["edits"]
    corners = [e for e in edits if e["rule"] == "sharp_bed_corners"]
    # Four outer corners kept; the two slot mouth corners at x=17, 23 (y=0) are skipped.
    assert len(corners) == 4
    assert all(not (15.0 < _edit_pts(e)[:, 0].mean() < 25.0 and _edit_pts(e)[:, 1].mean() < 5.0) for e in corners)
    assert any("Left the sized fit alone." in e["description"] for e in corners)
    foot = next(e for e in edits if e["id"] == "foot-chamfer")
    assert "Left the sized fit alone." in foot["description"]

    # 4. Gear with a sized fit on its central bore: both teeth and bore skipped -> no foot chamfer.
    api.load((CORPUS / "gear.stl").read_bytes(), "gear_fit")
    bore = next(f for f in api.design(UP, {})["features"] if f["type"] == "hole")
    api.set_fit(bore["region"], UP, 8.0, "sliding")
    edits = api.design(UP, {"material": "abs"})["edits"]
    assert not [e for e in edits if e["rule"] in ("sharp_bed_corners", "elephant_foot")]


def _external_thread_mesh(major=8.0, pitch=1.25, length=10.0, n_theta=72, per_pitch=16) -> Mesh:
    """Closed external 60° threaded rod along z starting at z = 0."""
    depth = 0.6 * pitch
    th = np.linspace(0, 2 * np.pi, n_theta, endpoint=False)
    zs = np.linspace(0, length, int(length / pitch * per_pitch) + 1)
    T, Z = np.meshgrid(th, zs)
    s = ((Z - T / (2 * np.pi) * pitch) / pitch) % 1.0
    R = major / 2 - depth + depth * np.interp(s, [0, 0.15, 0.5, 0.65, 1.0], [0, 0, 1, 1, 0])
    P = np.stack([R * np.cos(T), R * np.sin(T), Z], axis=-1)
    nz, nt = P.shape[:2]
    i, j = np.meshgrid(np.arange(nz - 1), np.arange(nt), indexing="ij")
    a, b, c, d = P[i, j], P[i, (j + 1) % nt], P[i + 1, (j + 1) % nt], P[i + 1, j]
    side = np.concatenate([np.stack([a, b, c], 2).reshape(-1, 3, 3), np.stack([a, c, d], 2).reshape(-1, 3, 3)])
    caps = []
    for k, flip in ((0, True), (nz - 1, False)):
        ring = P[k]
        ctr = np.broadcast_to([0, 0, zs[k]], ring.shape)
        tri = np.stack([ctr, ring, np.roll(ring, -1, axis=0)], 1)
        caps.append(tri[:, ::-1] if flip else tri)
    return Mesh(np.concatenate([side, *caps]))


def test_detected_threads_left_alone():
    # 1. External threaded rod standing on the bed: no corner rounding, no foot chamfer.
    api.load(to_binary_stl(_external_thread_mesh()), "m8_bolt")
    edits = api.design(UP, {"material": "abs"})["edits"]
    assert not [e for e in edits if e["rule"] in ("sharp_bed_corners", "elephant_foot")]

    # 2. Block with a through-threaded hole reaching the bed: outer block edited, thread skipped.
    api.load(to_binary_stl(_through_threaded_block()), "m10_nut_block")
    edits = api.design(UP, {"material": "abs"})["edits"]
    assert sum(e["rule"] == "sharp_bed_corners" for e in edits) == 4
    foot = next(e for e in edits if e["id"] == "foot-chamfer")
    pts = _edit_pts(foot)
    assert np.hypot(pts[:, 0], pts[:, 1]).min() > 8.0  # outer 24 mm square only, not the M10 thread
    assert "Left the threads alone." in foot["description"]

