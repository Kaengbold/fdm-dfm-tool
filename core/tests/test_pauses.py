"""Pause to put something into a closed pocket while printing (T-322)."""

import io
import zipfile

import numpy as np
import pytest

from fdmcore import api
from fdmcore.mesh import Mesh, to_binary_stl

from test_formats import box

UP = [0, 0, 1]


def _block(pocket_size=10.0) -> bytes:
    """30 × 30 × 10 mm block with a closed pocket (an inner shell) from z = 4 to 7 mm."""
    lo = 15 - pocket_size / 2
    pocket = box([0, 0, 0], 1, inward=True) * [pocket_size, pocket_size, 3] + [lo, lo, 4]
    return to_binary_stl(Mesh(np.concatenate([box([0, 0, 0], 1) * [30, 30, 10], pocket])))


def test_pocket_gets_a_pause_before_its_roof():
    api.load(_block(), "magnet_block")
    found = [f for f in api.check(UP, {"material": "pla"})["findings"] if f["rule"] == "pause_to_insert"]
    assert len(found) == 1
    f = found[0]
    layer_h = 0.2
    assert f["metrics"]["pause_z"] == pytest.approx(7.0 + layer_h) and f["severity"] == "info"
    assert f"layer {f['metrics']['layer']}" in f["detail"] and f["metrics"]["top_z"] == pytest.approx(7.0)


def test_long_roof_is_a_warning():
    api.load(_block(pocket_size=28.0), "wide")  # 28 mm roof, PLA bridges about 35 mm cleanly...
    f = next(f for f in api.check(UP, {"material": "pla"})["findings"] if f["rule"] == "pause_to_insert")
    assert f["severity"] == "info"
    api.load(to_binary_stl(Mesh(np.concatenate([box([0, 0, 0], 1) * [60, 60, 10],
                                                box([0, 0, 0], 1, inward=True) * [50, 50, 3] + [5, 5, 4]]))), "wider")
    f = next(f for f in api.check(UP, {"material": "pla"})["findings"] if f["rule"] == "pause_to_insert")
    assert f["severity"] == "warn" and "sag" in f["detail"]  # ...but not 50 mm


def test_plain_solid_has_no_pocket():
    api.load(to_binary_stl(Mesh(box([0, 0, 0], 20))), "cube")
    assert not [f for f in api.check(UP, {"material": "pla"})["findings"] if f["rule"] == "pause_to_insert"]


def test_export_carries_the_pause_for_each_slicer():
    api.load(_block(), "magnet_block")
    for slicer in ("orca", "prusa"):
        z = zipfile.ZipFile(io.BytesIO(api.export(UP, {}, {}, slicer=slicer, pauses=[7.0])))  # the pocket's top
        orca = z.read("Metadata/custom_gcode_per_layer.xml").decode()
        prusa = z.read("Metadata/Prusa_Slicer_custom_gcode_per_print_z.xml").decode()
        # Orca pauses at the nearest layer (7.0 + 0.6 × 0.2), Prusa at the next one up (just above 7.0).
        assert 'top_z="7.120" type="1"' in orca and 'print_z="7.010" type="1"' in prusa
    plain = zipfile.ZipFile(io.BytesIO(api.export(UP, {}, {})))
    assert "Metadata/custom_gcode_per_layer.xml" not in plain.namelist()
