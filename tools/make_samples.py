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


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for name, fn in {"mushroom": mushroom, "l_bracket": l_bracket, "hook": hook, "test_block": test_block}.items():
        m = fn()
        assert m.is_watertight, name
        m.export(OUT / f"{name}.stl")
        print(f"{name}: {len(m.faces)} faces, volume {m.volume:.0f} mm³")
