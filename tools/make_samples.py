"""Generate demo/test STL parts. Dev-only: needs `pip install trimesh manifold3d`.

Usage: python tools/make_samples.py
"""

from pathlib import Path

import trimesh
from trimesh.creation import box, cylinder

OUT = Path(__file__).resolve().parents[1] / "web" / "samples"


def at(mesh, x=0.0, y=0.0, z=0.0):
    mesh.apply_translation([x, y, z])
    return mesh


def mushroom():
    """Wide cap on a thin stem — as modelled it needs big supports; flipped it doesn't."""
    stem = at(box([10, 10, 20]), z=10)
    cap = at(box([40, 40, 6]), z=23)
    return trimesh.boolean.union([stem, cap], engine="manifold")


def l_bracket():
    """L-bracket with a horizontal hole in the upright and a vertical hole in the base."""
    base = at(box([60, 30, 5]), z=2.5)
    upright = at(box([5, 30, 40]), x=-27.5, z=20)
    body = trimesh.boolean.union([base, upright], engine="manifold")
    h_hole = cylinder(radius=4, height=20, sections=48)
    h_hole.apply_transform(trimesh.transformations.rotation_matrix(1.5708, [0, 1, 0]))
    at(h_hole, x=-27.5, z=28)
    v_hole = at(cylinder(radius=3, height=20, sections=48), x=10)
    return trimesh.boolean.difference([body, h_hole, v_hole], engine="manifold")


def hook():
    """Wall hook: back plate with an arm sticking out and a lip — classic layer-strength case."""
    plate = at(box([20, 4, 50]), z=25)
    arm = at(box([20, 30, 5]), y=17, z=8)
    lip = at(box([20, 4, 12]), y=30, z=16)
    return trimesh.boolean.union([plate, arm, lip], engine="manifold")


def test_block():
    """Thin fin (0.6 mm), too-thin fin (0.3 mm) and a 30 mm bridge on one base."""
    base = at(box([60, 30, 3]), z=1.5)
    fin = at(box([0.6, 20, 12]), x=-24, z=9)
    hair = at(box([0.3, 20, 12]), x=-18, z=9)
    left = at(box([6, 10, 15]), x=-3, z=10.5)
    right = at(box([6, 10, 15]), x=27, z=10.5)
    beam = at(box([36, 10, 3]), x=12, z=19.5)
    return trimesh.boolean.union([base, fin, hair, left, right, beam], engine="manifold")


def enclosure():
    """Open-top electronics box with vent slots and screw bosses — should print open side up."""
    outer = at(box([80, 60, 40]), z=20)
    inner = at(box([76, 56, 40]), z=22)
    shell = trimesh.boolean.difference([outer, inner], engine="manifold")
    bosses = [at(cylinder(radius=3.5, height=20, sections=32), x=sx * 32, y=sy * 22, z=12) for sx in (-1, 1) for sy in (-1, 1)]
    holes = [at(cylinder(radius=1.4, height=20, sections=24), x=sx * 32, y=sy * 22, z=14) for sx in (-1, 1) for sy in (-1, 1)]
    slots = [at(box([4, 10, 20]), x=-25 + 10 * i, y=-30, z=25) for i in range(6)]
    part = trimesh.boolean.union([shell, *bosses], engine="manifold")
    return trimesh.boolean.difference([part, *holes, *slots], engine="manifold")


def phone_stand():
    """Side profile of a phone stand extruded 70 mm — should lie on its big flat side or base."""
    import numpy as np
    from shapely.geometry import Polygon

    profile = Polygon([(0, 0), (90, 0), (90, 6), (30, 6), (60, 70), (54, 73), (22, 6), (14, 6), (14, 18), (8, 18), (8, 6), (0, 6)])
    return trimesh.creation.extrude_polygon(profile, 70)


def cable_clip():
    """C-shaped clip with a screw tab — flat on its side is best."""
    from shapely.geometry import Point, box as sbox

    ring = Point(0, 0).buffer(8, 64).difference(Point(0, 0).buffer(5.5, 64)).difference(sbox(-3, 4, 3, 10))
    tab = sbox(-4, -14, 4, -7.5).union(sbox(-1.6, -8, 1.6, -7.9))
    return trimesh.creation.extrude_polygon(ring.union(tab), 10)


def detail_plate():
    """Small-feature test: Ø1/Ø3/Ø5 holes, a 0.15 mm slot, engraved lines on the bottom, an upright pin."""
    plate = at(box([60, 40, 4]), z=2)
    holes = [at(cylinder(radius=d / 2, height=10, sections=48), x=x, y=10) for x, d in ((-20, 1.0), (-10, 3.0), (2, 5.0))]
    slot = at(box([0.15, 12, 10]), x=15, y=10)
    grooves = [at(box([0.4, 14, 0.8]), x=-20 + 3 * i, y=-10, z=0) for i in range(4)]  # engraved "text" on the bed side
    pin = at(cylinder(radius=2, height=20, sections=32), x=20, y=-10, z=14)
    part = trimesh.boolean.union([plate, pin], engine="manifold")
    return trimesh.boolean.difference([part, *holes, slot, *grooves], engine="manifold")


def dumbbell():
    """Two blocks joined by a thin neck — the neck is the weak point when printed upright."""
    low = at(box([30, 30, 15]), z=7.5)
    neck = at(box([6, 6, 6]), z=18)
    high = at(box([30, 30, 15]), z=28.5)
    return trimesh.boolean.union([low, neck, high], engine="manifold")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for name, fn in {
        "mushroom": mushroom,
        "l_bracket": l_bracket,
        "hook": hook,
        "test_block": test_block,
        "enclosure": enclosure,
        "phone_stand": phone_stand,
        "cable_clip": cable_clip,
        "detail_plate": detail_plate,
        "dumbbell": dumbbell,
    }.items():
        m = fn()
        assert m.is_watertight, name
        m.export(OUT / f"{name}.stl")
        print(f"{name}: {len(m.faces)} faces, volume {m.volume:.0f} mm³")
