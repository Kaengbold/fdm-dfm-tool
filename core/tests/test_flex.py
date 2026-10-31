"""Living hinges, snap-fit tabs and thick blocks (T-181)."""

import numpy as np
import shapely
from shapely.geometry.polygon import orient as orient_poly
from shapely.ops import unary_union
from shapely.geometry import box

from fdmcore.additions import extrude
from fdmcore.checks import orient, run_checks
from fdmcore.mesh import Mesh
from fdmcore.profile import Profile


def prism(poly, depth: float) -> np.ndarray:
    """Closed prism of a polygon without holes (constrained triangulation: thin profiles defeat the
    engine's Delaunay-only ``extrude``)."""
    poly = orient_poly(poly.simplify(0), 1.0)
    caps = shapely.get_coordinates(shapely.get_parts(shapely.constrained_delaunay_triangles(poly))).reshape(-1, 4, 2)[:, :3]
    cross = np.cross(caps[:, 1] - caps[:, 0], caps[:, 2] - caps[:, 0])
    caps = np.where((cross < 0)[:, None, None], caps[:, ::-1], caps)
    lift = lambda t, z: np.concatenate([t, np.full((*t.shape[:2], 1), z)], axis=2)
    ring = np.asarray(poly.exterior.coords)
    a, b = ring[:-1], ring[1:]
    walls = [np.stack([lift(a[:, None], 0)[:, 0], lift(b[:, None], 0)[:, 0], lift(b[:, None], depth)[:, 0]], 1),
             np.stack([lift(a[:, None], 0)[:, 0], lift(b[:, None], depth)[:, 0], lift(a[:, None], depth)[:, 0]], 1)]
    return np.concatenate([lift(caps[:, ::-1], 0), lift(caps, depth), *walls])


def side_profile(boxes, depth: float, axes) -> Mesh:
    """A 2D profile (union of ``boxes``) made ``depth`` deep, its axes placed by ``axes``: (profile u,
    profile v, depth) to x, y, z columns; odd permutations get their triangles flipped."""
    t = prism(unary_union(boxes), depth)[..., list(axes)]
    if axes in ((0, 2, 1), (2, 1, 0), (1, 0, 2)):
        t = t[:, [0, 2, 1]]
    return orient(Mesh(t), np.eye(3))


HINGE = [box(0, 0, 20, 5), box(20, 0, 22, 0.5), box(22, 0, 42, 5)]


def findings(mesh, material="pla"):
    return [f for f in run_checks(mesh, Profile(material_id=material))["findings"] if f["rule"] in ("living_hinge", "snap_fit_upright", "thick_solid_block")]


def test_flat_hinge_found_and_material_matters():
    part = side_profile(HINGE, 20, (0, 2, 1))  # profile in x-z, 20 mm deep along y
    pla = findings(part)
    assert [f["rule"] for f in pla] == ["living_hinge"]
    assert pla[0]["severity"] == "warn" and abs(pla[0]["metrics"]["thickness_mm"] - 0.5) < 0.25
    assert findings(part, "petg")[0]["severity"] == "info"


def test_upright_hinge_is_a_problem():
    part = side_profile(HINGE, 20, (1, 2, 0))  # the same part standing: the strip joins blocks above and below
    found = findings(part)
    assert [(f["rule"], f["severity"]) for f in found] == [("living_hinge", "problem")]


def test_upright_snap_fit_with_hook():
    tab = [box(0, 0, 10, 5), box(4, 5, 5.5, 15), box(4, 15, 5.5, 17), box(5.5, 15, 6.5, 17)]  # base, tab, hook (y-z profile)
    found = findings(side_profile(tab, 10, (2, 0, 1)))
    assert [f["rule"] for f in found] == ["snap_fit_upright"]
    m = found[0]["metrics"]
    assert abs(m["thickness_mm"] - 1.5) < 0.2 and abs(m["hook_mm"] - 1.0) < 0.2
    assert m["hook_max_mm"] < m["hook_max_flat_mm"]
    no_hook = [box(0, 0, 10, 5), box(4, 5, 5.5, 17)]
    assert not findings(side_profile(no_hook, 10, (2, 0, 1)))


def test_thick_block_and_thin_box():
    found = findings(orient(Mesh(extrude(box(0, 0, 40, 40), 0, 30)), np.eye(3)))
    assert [f["rule"] for f in found] == ["thick_solid_block"]
    assert found[0]["metrics"]["thick_cm3"] > 15
    assert not findings(orient(Mesh(extrude(box(0, 0, 40, 40).difference(box(3, 3, 37, 37)), 0, 30)), np.eye(3)))
