"""Generate the regression corpus: typical hobby parts with a known good orientation (T-003).

Stand-ins built from primitives, so there is no licence question; the expected orientation and
key findings per part live in core/tests/test_corpus.py. Dev-only: needs `pip install trimesh manifold3d`.

Usage: python tools/make_corpus.py
"""

import math
from pathlib import Path

import trimesh
from trimesh.creation import box, cylinder

OUT = Path(__file__).resolve().parents[1] / "core" / "tests" / "corpus"
U = lambda *parts: trimesh.boolean.union(list(parts), engine="manifold")
D = lambda *parts: trimesh.boolean.difference(list(parts), engine="manifold")


def at(mesh, x=0.0, y=0.0, z=0.0):
    mesh.apply_translation([x, y, z])
    return mesh


def turned(mesh, axis, deg):
    mesh.apply_transform(trimesh.transformations.rotation_matrix(math.radians(deg), axis))
    return mesh


def prism(profile_xz, depth):
    """A 2D profile in the XZ plane extruded ``depth`` along Y (centred)."""
    poly = trimesh.path.polygons.Polygon(profile_xz)
    m = trimesh.creation.extrude_polygon(poly, depth)  # profile in XY, extruded along Z
    turned(m, [1, 0, 0], 90)  # XY profile -> XZ, extrusion -> -Y
    return at(m, y=depth / 2)


def shelf_bracket():
    """L bracket with a 45° gusset, modelled standing on its base; strongest lying on its side."""
    return prism([(0, 0), (60, 0), (60, 5), (12, 5), (5, 12), (5, 60), (0, 60)], 20)


def knob():
    """Round knob with a grip skirt and a D-shaft hole from below."""
    body = U(at(cylinder(radius=15, height=12, sections=64), z=6), at(cylinder(radius=8, height=8, sections=48), z=16))
    shaft = at(cylinder(radius=3.1, height=14, sections=32), z=7)
    flat = at(box([2, 8, 14]), x=2.6, z=7)
    return D(body, D(shaft, flat))


def pencil_cup():
    """Open cylinder: prints open side up, no support."""
    return D(at(cylinder(radius=35, height=90, sections=96), z=45), at(cylinder(radius=32, height=90, sections=96), z=47))


def drawer_handle():
    """Bar on two posts, modelled posts down: the bar would be a long bridge; on its side it's a flat profile."""
    return prism([(0, 0), (10, 0), (10, 15), (90, 15), (90, 0), (100, 0), (100, 25), (0, 25)], 12)


def text_sign():
    """Name plate with raised bars standing in for letters: plate down, bars up."""
    plate = at(box([100, 40, 3]), z=1.5)
    bars = [at(box([4, 20, 2]), x=-40 + 9 * i, z=4) for i in range(9)]
    return U(plate, *bars)


def pegboard_hook():
    """Flat J hook profile: printed lying flat so the layers run along the curve."""
    return prism([(0, 0), (6, 0), (6, 40), (30, 40), (30, 30), (36, 30), (36, 46), (0, 46)], 8)


def spool_hub():
    """Flanged hub with a bearing bore: flange on the bed."""
    hub = U(at(cylinder(radius=30, height=4, sections=96), z=2), at(cylinder(radius=20, height=30, sections=96), z=19))
    return D(hub, at(cylinder(radius=11, height=40, sections=64), z=17))


def tray():
    """Shallow tray: open side up."""
    return D(at(box([120, 80, 20]), z=10), at(box([114, 74, 20]), z=13))


def tall_pin():
    """Tall thin pin on a small foot, modelled standing: tips and wobbles; lying down it's fine."""
    return U(at(cylinder(radius=3, height=80, sections=32), z=40), at(cylinder(radius=5, height=3, sections=32), z=1.5))


def gear():
    """Flat spur gear stand-in (square teeth) with a bore: flat on the bed."""
    teeth = [turned(at(box([4, 4, 8]), x=24, z=4), [0, 0, 1], i * 360 / 20) for i in range(20)]
    disk = at(cylinder(radius=23, height=8, sections=96), z=4)
    return D(U(disk, *teeth), at(cylinder(radius=4, height=20, sections=32), z=4))


PARTS = {f.__name__: f for f in (shelf_bracket, knob, pencil_cup, drawer_handle, text_sign, pegboard_hook, spool_hub, tray, tall_pin, gear)}

if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for name, fn in PARTS.items():
        m = fn()
        assert m.is_watertight, name
        m.export(OUT / f"{name}.stl")
        print(f"{name}: {len(m.faces)} faces, {m.extents.round(1)} mm")
