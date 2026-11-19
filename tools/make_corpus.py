"""Generate the regression corpus: typical hobby parts with a known good orientation (T-003).

Stand-ins built from primitives, so there is no licence question; the expected orientation and
key findings per part live in core/tests/test_corpus.py. Dev-only: needs `pip install trimesh manifold3d`.

Usage: python tools/make_corpus.py
"""

import math
from pathlib import Path

import numpy as np
import trimesh
from shapely.geometry import box as sbox
from trimesh.creation import box, cylinder, uv_sphere

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


def bumpy_blob(n: int = 100):
    """Closed organic sculpt stand-in: bumpy sphere scaled to 80 mm tall (2*n*n triangles)."""
    lon = np.linspace(0.0, 2.0 * np.pi, n, endpoint=False)
    lat = np.linspace(0.0, np.pi, n + 2)[1:-1]
    az, po = np.meshgrid(lon, lat)
    r = 30.0 * (1.0 + 0.15 * np.sin(7.0 * az) * np.sin(5.0 * po) + 0.05 * np.sin(23.0 * az + 11.0 * po))
    rings = r[..., None] * np.stack([np.sin(po) * np.cos(az), np.sin(po) * np.sin(az), np.cos(po)], axis=-1)
    # Single north/south pole vertices keep the surface strictly closed and watertight.
    verts = np.vstack([[[0.0, 0.0, 30.0]], rings.reshape(-1, 3), [[0.0, 0.0, -30.0]]])
    verts[:, 2] *= 80.0 / np.ptp(verts[:, 2])
    verts[:, 2] -= verts[:, 2].min()

    cols = np.arange(n)
    nxt = (cols + 1) % n
    top = np.column_stack([np.zeros(n, dtype=int), 1 + cols, 1 + nxt])
    south = len(verts) - 1
    bot_start = 1 + (n - 1) * n
    bot = np.column_stack([np.full(n, south, dtype=int), bot_start + nxt, bot_start + cols])
    row, col = np.meshgrid(np.arange(n - 1), cols, indexing="ij")
    a = 1 + row * n + col
    b = 1 + row * n + (col + 1) % n
    c, d = a + n, b + n
    faces = np.vstack([top, np.stack([a, c, b], -1).reshape(-1, 3), np.stack([b, c, d], -1).reshape(-1, 3), bot])
    m = trimesh.Trimesh(vertices=verts, faces=faces, process=False)
    if m.volume < 0:
        m.invert()
    return m


def bumpy_blob_20k():
    """Organic bumpy blob at 20,000 triangles."""
    return bumpy_blob(100)


def bumpy_blob_80k():
    """Organic bumpy blob at 80,000 triangles (heavy-mesh timing and scaling test)."""
    return bumpy_blob(200)


def ribbed_housing():
    """Filleted open-top housing with internal cross stiffening ribs: prints open side up."""
    w, d, h, r, wall, floor, sections = 90.0, 65.0, 35.0, 4.0, 2.5, 3.0, 32
    iw, id_ = w - 2 * r, d - 2 * r
    outer_parts = [
        at(box([iw, d, h - r]), z=r + (h - r) / 2),
        at(box([w, id_, h - r]), z=r + (h - r) / 2),
        at(box([iw, id_, h]), z=h / 2),
    ]
    for sx in (-1, 1):
        for sy in (-1, 1):
            outer_parts.append(at(cylinder(radius=r, height=h - r, sections=sections), x=sx * iw / 2, y=sy * id_ / 2, z=r + (h - r) / 2))
            outer_parts.append(at(uv_sphere(radius=r, count=[sections // 2, sections // 2]), x=sx * iw / 2, y=sy * id_ / 2, z=r))
    for sy in (-1, 1):
        outer_parts.append(at(turned(cylinder(radius=r, height=iw, sections=sections), [0, 1, 0], 90), y=sy * id_ / 2, z=r))
    for sx in (-1, 1):
        outer_parts.append(at(turned(cylinder(radius=r, height=id_, sections=sections), [1, 0, 0], 90), x=sx * iw / 2, z=r))
    outer = U(*outer_parts)

    ri = max(r - wall, 1.5)
    cw, cd = w - 2 * wall, d - 2 * wall
    cavity_poly = sbox(-cw / 2 + ri, -cd / 2 + ri, cw / 2 - ri, cd / 2 - ri).buffer(ri, quad_segs=sections // 4)
    cavity = at(trimesh.creation.extrude_polygon(cavity_poly, h - floor + 2.0), z=floor)
    shell = D(outer, cavity)

    rib_h, rib_t = 14.0, 2.0
    ribs = [
        at(box([rib_t, cd + 1.0, rib_h]), x=-20.0, z=floor + rib_h / 2 - 0.5),
        at(box([rib_t, cd + 1.0, rib_h]), x=20.0, z=floor + rib_h / 2 - 0.5),
        at(box([cw + 1.0, rib_t, rib_h]), y=0.0, z=floor + rib_h / 2 - 0.5),
    ]
    return U(shell, *ribs)


PARTS = {
    f.__name__: f
    for f in (
        shelf_bracket, knob, pencil_cup, drawer_handle, text_sign, pegboard_hook,
        spool_hub, tray, tall_pin, gear, bumpy_blob_20k, bumpy_blob_80k, ribbed_housing,
    )
}

if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for name, fn in PARTS.items():
        m = fn()
        assert m.is_watertight, name
        m.export(OUT / f"{name}.stl")
        print(f"{name}: {len(m.faces)} faces, {m.extents.round(1)} mm")
