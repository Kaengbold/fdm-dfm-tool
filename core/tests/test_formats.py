import io
import zipfile

import numpy as np
import pytest

from fdmcore import api
from fdmcore.formats import read_3mf, split_bodies, unit_hint
from fdmcore.mesh import Mesh, to_binary_stl

# Unit cube, outward winding.
CUBE_V = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], float)
CUBE_F = np.array([[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4],
                   [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]])


def box(lo, size, inward=False):
    tri = CUBE_V[CUBE_F] * size + lo
    return tri[:, ::-1] if inward else tri


def test_split_two_boxes_and_keep_cavity():
    hollow = np.concatenate([box([0, 0, 0], 20), box([5, 5, 5], 10, inward=True)])
    small = box([40, 0, 0], 10)
    bodies = split_bodies(Mesh(np.concatenate([small, hollow])))
    assert [b.face_count for b in bodies] == [24, 12]  # hollow box (with its cavity) first: bigger
    assert bodies[0].volume() == pytest.approx(8000 - 1000)


def test_crumbs_are_dropped():
    m = Mesh(np.concatenate([box([0, 0, 0], 20), box([30, 0, 0], 0.5)]))
    assert len(split_bodies(m)) == 1


def model_xml(objects, items, unit="millimeter"):
    res = ""
    for oid, tri in objects.items():
        v = tri.reshape(-1, 3)
        res += f'<object id="{oid}" name="part{oid}" type="model"><mesh><vertices>'
        res += "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in v)
        res += "</vertices><triangles>" + "".join(f'<triangle v1="{3*i}" v2="{3*i+1}" v3="{3*i+2}"/>' for i in range(len(tri)))
        res += "</triangles></mesh></object>"
    build = "".join(f'<item objectid="{oid}" transform="{t}"/>' for oid, t in items)
    return (f'<?xml version="1.0"?><model unit="{unit}" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
            f"<resources>{res}</resources><build>{build}</build></model>")


def make_3mf(xml):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("3D/3dmodel.model", xml)
    return buf.getvalue()


def test_3mf_objects_transforms_and_units():
    xml = model_xml({1: box([0, 0, 0], 1), 2: box([0, 0, 0], 2)},
                    [(1, "1 0 0 0 1 0 0 0 1 10 0 0"), (2, "1 0 0 0 1 0 0 0 1 0 0 0")], unit="centimeter")
    bodies = read_3mf(make_3mf(xml))
    assert [n for n, _ in bodies] == ["part1", "part2"]
    lo, hi = bodies[0][1].bounds()
    assert lo.tolist() == [100, 0, 0] and hi.tolist() == [110, 10, 10]


def test_load_multi_body_and_select():
    stl = to_binary_stl(Mesh(np.concatenate([box([0, 0, 0], 10), box([20, 0, 0], 20)])))
    info = api.load(stl, "plate")
    assert len(info["bodies"]) == 2 and info["size_mm"] == [20, 20, 20]
    assert api.select_body(1)["size_mm"] == [10, 10, 10]


def test_unit_hint_and_scale():
    info = api.load(to_binary_stl(Mesh(box([0, 0, 0], 2))), "inch_part")
    assert info["unit_hint"]["factor"] == 25.4
    assert api.scale(25.4)["size_mm"] == pytest.approx([50.8] * 3)
    assert unit_hint([30, 20, 10]) is None
    assert unit_hint([0.08, 0.05, 0.02])["factor"] == 1000


def test_3mf_components_in_other_file():
    """Bambu/Orca style: the build item points at a component stored in another model file."""
    sub = model_xml({7: box([0, 0, 0], 5)}, [])
    root = ('<?xml version="1.0"?><model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
            'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06"><resources>'
            '<object id="1" name="bracket" type="model"><components>'
            '<component p:path="/3D/Objects/object_1.model" objectid="7" transform="1 0 0 0 1 0 0 0 1 0 0 3"/>'
            '</components></object></resources><build><item objectid="1"/></build></model>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("3D/3dmodel.model", root)
        z.writestr("3D/Objects/object_1.model", sub)
    (name, mesh), = read_3mf(buf.getvalue())
    assert name == "bracket" and mesh.bounds()[0].tolist() == [0, 0, 3]
