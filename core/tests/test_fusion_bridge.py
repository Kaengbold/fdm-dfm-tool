"""The Fusion add-in's adsk-free half (fusion/FDMDFM/bridge.py)."""

import importlib.util
import math
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("fusion_bridge", ROOT / "fusion" / "FDMDFM" / "bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


def _inside(box, p):
    """Point in an OrientedBoundingBox3D-style box (height along length_dir × width_dir)."""
    d = np.asarray(p) - box["center"]
    length, width = np.asarray(box["length_dir"]), np.asarray(box["width_dir"])
    return (abs(d @ length) <= box["length"] / 2 and abs(d @ width) <= box["width"] / 2
            and abs(d @ np.cross(length, width)) <= box["height"] / 2)


def test_teardrop_boxes_make_a_45_degree_roof():
    r = 4.0
    hole = {"axis": [1, 0, 0], "radius": r, "center": [10, 0, 5], "length": 20}
    diamond, upper = bridge.teardrop_boxes(hole, [0, 0, 1])
    roof = lambda y, z: _inside(diamond, [10, y, 5 + z]) and _inside(upper, [10, y, 5 + z])
    assert roof(0, r * math.sqrt(2) * 0.98)  # the tip
    assert not roof(0, r * math.sqrt(2) * 1.05)
    assert not roof(0, -r * 0.9) and not roof(r * 0.9, 0)  # nothing below or beside the hole
    # The roof's sides are 45°: at height z its half-width is r√2 − z, and it adds material only outside the hole.
    for z in np.linspace(r / math.sqrt(2), r * math.sqrt(2) * 0.95, 6):
        half = r * math.sqrt(2) - z
        assert roof(half * 0.98, z) and not roof(half * 1.02 + 0.01, z)
    # Along the axis it runs a little past both ends of the hole.
    assert _inside(upper, [10 + 10.5, 0, 5 + r]) and not _inside(upper, [10 + 11.5, 0, 5 + r])


def test_analyse_finds_the_bracket_hole_and_report_html_escapes():
    stl = (ROOT / "web" / "samples" / "l_bracket.stl").read_bytes()
    result = bridge.analyse(stl, "bambu_p1s", "pla")
    assert result["up"] in ([0.0, 0.0, 1.0], [0.0, -1.0, 0.0], [0.0, 1.0, 0.0])  # on its base, or on its side (PLA: no footprint bonus)
    assert len(result["teardrops"]) == (1 if result["up"][2] else 2)  # on its side, the base's hole lies sideways too
    for hole in result["teardrops"]:
        assert abs(np.dot(hole["axis"], result["up"])) < 0.01 and hole["radius"] > 0  # lying sideways in the print
    page = bridge.report_html("# Title\n- **bold** <x>\n| a | b |\n|---|---|", result["up"])
    assert "<h1>Title</h1>" in page and "<li><b>bold</b> &lt;x&gt;</li>" in page and "a · b" in page


def test_no_teardrop_for_an_upright_hole():
    assert bridge.teardrop_boxes({"axis": [0, 0, 1], "radius": 2.0, "center": [0, 0, 0], "length": 5}, [0, 0, 1]) is None
