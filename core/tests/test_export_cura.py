"""Cura reads per-model settings from ``cura:`` metadata on the 3MF object (T-452).

Cura isn't run here: the test reads the file the way Cura's 3MFReader / libSavitar does (the object's
``metadatagroup``, the ``cura`` namespace stripped) and checks keys and values. tools/slicer_check.py
checks that OrcaSlicer, Bambu Studio and PrusaSlicer still apply their own settings.
"""

import io
import zipfile
import xml.etree.ElementTree as ET

import numpy as np

from fdmcore.export3mf import CURA_NS, cura_settings, export_3mf
from fdmcore.mesh import Mesh

CORE = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
# Cura 5.9 keys with settable_per_mesh = true (resources/definitions/fdmprinter.def.json).
PER_MESH = {"wall_line_count", "infill_sparse_density", "infill_pattern", "top_layers", "bottom_layers", "support_enable",
            "support_angle", "hole_xy_offset", "xy_offset", "xy_offset_layer_0", "ironing_enabled", "ironing_only_highest_layer",
            "bridge_settings_enabled", "bridge_wall_speed", "bridge_skin_speed", "magic_fuzzy_skin_enabled",
            "magic_fuzzy_skin_outside_only", "magic_fuzzy_skin_thickness", "magic_fuzzy_skin_point_dist"}
SETTINGS = {"layer_height": "0.2", "wall_loops": "4", "sparse_infill_density": "25%", "sparse_infill_pattern": "gyroid",
            "top_shell_layers": "5", "bottom_shell_layers": "4", "enable_support": "1", "support_type": "tree(auto)",
            "support_threshold_angle": "35", "brim_type": "outer_only", "brim_width": "5", "xy_hole_compensation": "0.1",
            "elefant_foot_compensation": "0.15", "ironing_type": "topmost", "bridge_speed": "25", "fuzzy_skin": "external",
            "fuzzy_skin_thickness": "0.30", "fuzzy_skin_point_distance": "0.8"}


def cube():
    v = np.array([[0, 0, 0], [10, 0, 0], [10, 10, 0], [0, 10, 0], [0, 0, 10], [10, 0, 10], [10, 10, 10], [0, 10, 10]], float) - [5, 5, 0]
    f = [[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]]
    return Mesh(v[np.array(f)])


def cura_read(data: bytes) -> dict[str, str]:
    """Per-model settings as Cura's reader sees them on the first object."""
    model = ET.fromstring(zipfile.ZipFile(io.BytesIO(data)).read("3D/3dmodel.model"))
    obj = model.find(f"{CORE}resources/{CORE}object")
    group = obj.find(f"{CORE}metadatagroup")
    assert list(obj).index(group) < list(obj).index(obj.find(f"{CORE}mesh")), "3MF core spec: metadatagroup before mesh"
    return {m.get("name").removeprefix("cura:"): m.text for m in group.findall(f"{CORE}metadata")}


def test_cura_gets_the_per_model_settings():
    got = cura_read(export_3mf(cube(), "cube", SETTINGS))
    assert got.keys() <= PER_MESH, got.keys() - PER_MESH
    assert got["wall_line_count"] == "4" and got["infill_sparse_density"] == "25" and got["infill_pattern"] == "gyroid"
    assert got["top_layers"] == "5" and got["bottom_layers"] == "4" and got["support_enable"] == "True"
    assert got["support_angle"] == "55"  # OrcaSlicer 35° from the horizontal = Cura 55° from the vertical
    assert got["xy_offset_layer_0"] == "-0.15" and got["hole_xy_offset"] == "0.1"
    assert got["ironing_enabled"] == got["ironing_only_highest_layer"] == "True"
    assert got["bridge_settings_enabled"] == "True" and got["bridge_wall_speed"] == got["bridge_skin_speed"] == "25"
    assert got["magic_fuzzy_skin_enabled"] == got["magic_fuzzy_skin_outside_only"] == "True"


def test_cura_namespace_is_declared():
    text = zipfile.ZipFile(io.BytesIO(export_3mf(cube(), "cube", SETTINGS))).read("3D/3dmodel.model").decode()
    assert f'xmlns:cura="{CURA_NS}"' in text


def test_global_and_unknown_settings_stay_out():
    assert not cura_settings({"layer_height": "0.2", "brim_type": "outer_only", "support_type": "tree(auto)",
                              "sparse_infill_pattern": "rectilinear", "ironing_type": "no ironing", "fuzzy_skin": "none"})
    assert cura_settings({"enable_support": "0"}) == {"support_enable": "False"}


def test_no_settings_no_metadata_group():
    model = zipfile.ZipFile(io.BytesIO(export_3mf(cube(), "cube"))).read("3D/3dmodel.model").decode()
    assert "metadatagroup" not in model
