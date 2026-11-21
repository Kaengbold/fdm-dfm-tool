"""A cooling plan in the 3MF for the user's own slicer (T-479): slower speeds per height, a cooling tower."""

import io
import re
import zipfile

import numpy as np
import pytest

from fdmcore import api
from fdmcore.export3mf import range_files
from test_hold import box

ORCA, PRUSA = "Metadata/layer_config_ranges.xml", "Metadata/Prusa_Slicer_layer_config_ranges.xml"


def ranges_in(text: str) -> list[tuple[float, float, dict]]:
    out = []
    for lo, hi, body in re.findall(r'<range min_z="([\d.]+)" max_z="([\d.]+)">(.*?)</range>', text, re.S):
        out.append((float(lo), float(hi), {k: float(v) for k, v in re.findall(r'opt_key="(\w+)">([\d.]+)<', body) if k != "extruder"}))
    return out


def test_overlapping_ranges_are_cut_where_they_meet():
    """Thinner layers over a roof (10–17 mm) and slower walls over 5–12 mm: the slicers take no overlaps, so
    three ranges, the middle one with both; every range has a layer height (the part's where it sets none)."""
    files = range_files([{"z0": 10, "z1": 17, "layer_height": 0.08}, {"z0": 5, "z1": 12, "outer_wall_speed": 23}], 0.2)
    assert ranges_in(files[ORCA]) == [(5, 10, {"outer_wall_speed": 23, "layer_height": 0.2}),
                                      (10, 12, {"layer_height": 0.08, "outer_wall_speed": 23}), (12, 17, {"layer_height": 0.08})]
    assert ranges_in(files[PRUSA])[1] == (10, 12, {"layer_height": 0.08, "external_perimeter_speed": 23})  # PrusaSlicer's names
    slower = range_files([{"z0": 0, "z1": 5, "outer_wall_speed": 40}, {"z0": 0, "z1": 5, "outer_wall_speed": 25}], 0.2)
    assert ranges_in(slower[ORCA]) == [(0, 5, {"outer_wall_speed": 25, "layer_height": 0.2})]  # the slower one wins
    joined = range_files([{"z0": 0, "z1": 5, "outer_wall_speed": 25}, {"z0": 5, "z1": 9, "outer_wall_speed": 25}], 0.2)
    assert ranges_in(joined[ORCA]) == [(0, 9, {"outer_wall_speed": 25, "layer_height": 0.2})]
    assert range_files([], 0.2) == {}


def plan_export(**cooling):
    return zipfile.ZipFile(io.BytesIO(api.export([0, 0, 1], {}, None, cooling=cooling)))


def test_a_cooling_plan_slows_its_heights_and_keeps_the_slowdown_on():
    api.load(box(20, 20, 30), "block")
    z = plan_export(ranges=[{"z0": 18, "z1": 30, "speeds": {"outer_wall": 25, "inner_wall": 30}}])
    layer = float(re.search(r'key="layer_height" value="([\d.]+)"', z.read("Metadata/model_settings.config").decode()).group(1))
    assert ranges_in(z.read(ORCA).decode()) == [(18, 30, {"outer_wall_speed": 25, "inner_wall_speed": 30, "layer_height": layer})]
    assert ranges_in(z.read(PRUSA).decode()) == [(18, 30, {"external_perimeter_speed": 25, "perimeter_speed": 30, "layer_height": layer})]
    everything = "".join(z.read(n).decode() for n in z.namelist() if n.endswith((".config", ".xml")))
    assert not re.search(r'key="(slow_down|fan_|cooling)', everything)  # the slicer's own cooling stays as it is
    for bad, words in (({"speeds": {"outer_wall": 25}, "fan": 100}, "fan change"), ({"speeds": {"bridges": 20}}, "Unknown speed"),
                       ({"speeds": {"outer_wall": 0}}, "above 0")):
        with pytest.raises(ValueError, match=words):
            plan_export(ranges=[{"z0": 5, "z1": 10, **bad}])


def test_a_cooling_tower_stands_beside_the_part():
    api.load(box(20, 20, 30), "block")
    z = plan_export(tower={"height": 22, "diameter": 8})
    model = z.read("3D/3dmodel.model").decode()
    v = np.array([[float(a), float(b), float(c)] for a, b, c in re.findall(r'<vertex x="([-\d.]+)" y="([-\d.]+)" z="([-\d.]+)"/>', model)])
    centre = v[:, :2].min(axis=0)[0] + 10  # the part is 20 mm wide, the tower beside it
    tower = v[v[:, 0] > centre + 10 + 1]
    assert len(tower) and tower[:, 2].max() == pytest.approx(22) and tower[:, 0].min() == pytest.approx(centre + 15, abs=0.01)
    with pytest.raises(ValueError, match="not both"):
        api.export([0, 0, 1], {}, None, copies=2, cooling={"tower": {"height": 10, "diameter": 8}})
    with pytest.raises(ValueError, match="height up to"):
        plan_export(tower={"height": 40, "diameter": 8})
