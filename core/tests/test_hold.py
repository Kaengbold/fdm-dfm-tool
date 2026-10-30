import numpy as np
import pytest

from fdmcore import api
from fdmcore.mesh import Mesh, to_binary_stl


def box(sx, sy, sz) -> bytes:
    v = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], float) * [sx, sy, sz]
    f = [[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]]
    return to_binary_stl(Mesh(v[np.array(f)]))


def region_facing(normal):
    return max(api.regions()["regions"], key=lambda r: np.dot(r["normal"], normal))["id"]


def test_resting_part_pressed_down_holds_lifted_part_is_refused():
    api.load(box(30, 10, 10), "block")
    bottom, top = region_facing([0, 0, -1]), region_facing([0, 0, 1])
    rest = {"region": bottom, "kind": "fixed", "hold": "resting"}
    pressed = api.strength([0, 0, 1], [rest, {"region": top, "kind": "force", "kg": 20, "direction": "push"}])
    assert np.isfinite(pressed["safety_factor"]) and pressed["safety_factor"] > 2
    with pytest.raises(ValueError, match="lifts off"):
        api.strength([0, 0, 1], [rest, {"region": top, "kind": "force", "kg": 20, "direction": "pull"}])


def test_resting_support_only_pushes():
    """A beam clamped at one end and resting on a ledge under the other: pushed down, the ledge
    carries it; pulled up, the beam lifts off the ledge and acts like it isn't there."""
    api.load(box(60, 10, 6), "beam")
    clamp = {"point": [0, 5, 3], "radius": 4, "kind": "fixed"}
    ledge = {"point": [55, 5, 0], "radius": 4, "kind": "fixed", "hold": "resting"}
    load = {"point": [30, 5, 6], "radius": 3, "kind": "force", "kg": 5}
    down = {**load, "direction": "push"}
    up = {**load, "direction": "pull"}
    free_down = api.strength([0, 0, 1], [clamp, down])["safety_factor"]
    rested_down = api.strength([0, 0, 1], [clamp, ledge, down])["safety_factor"]
    free_up = api.strength([0, 0, 1], [clamp, up])["safety_factor"]
    rested_up = api.strength([0, 0, 1], [clamp, ledge, up])["safety_factor"]
    assert rested_down > 1.5 * free_down
    assert rested_up == pytest.approx(free_up, rel=0.1)


def test_screw_in_a_hole_holds_a_plate():
    """A plate hanging on a screw through a hole, pulled at the far end: the hole wall carries it
    and the weak spot is around the hole."""
    from shapely.geometry import Point, box as sbox

    from fdmcore.additions import extrude

    api.load(to_binary_stl(Mesh(extrude(sbox(0, 0, 40, 15).difference(Point(7, 7.5).buffer(2.5, 32)), 0, 5))), "plate")
    regions = api.regions()["regions"]
    hole = min(regions, key=lambda r: np.linalg.norm(np.subtract(r["centroid"][:2], [7, 7.5])))["id"]
    far = region_facing([1, 0, 0])
    r = api.strength([0, 0, 1], [{"region": hole, "kind": "fixed", "hold": "screw"}, {"region": far, "kind": "force", "kg": 20, "direction": "pull"}])
    assert np.isfinite(r["safety_factor"]) and r["solver_converged"]
    assert r["critical_point"][0] < 16


def test_unknown_hold_is_refused():
    api.load(box(20, 10, 6), "bar")
    with pytest.raises(ValueError, match="hold must be"):
        api.strength([0, 0, 1], [{"region": region_facing([-1, 0, 0]), "kind": "fixed", "hold": "nailed"},
                                 {"region": region_facing([1, 0, 0]), "kind": "force", "kg": 1}])
