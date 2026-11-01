import numpy as np
from shapely.geometry import Point, Polygon

from fdmcore.additions import extrude
from fdmcore.mesh import Mesh
from fdmcore.regions import find_regions, region_info


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
