"""The UI compares with the engine's goal plan, and explicit export omissions stay omitted."""

import io
import json
import zipfile
from xml.etree import ElementTree as ET

import pytest
from shapely.geometry import box

from fdmcore import api
from fdmcore.additions import extrude
from fdmcore.mesh import Mesh, to_binary_stl
from fdmcore.profile import Profile
from fdmcore.recommend import goal_plan, recommend_settings


@pytest.mark.parametrize("goal", ["balanced", "speed", "strength", "material"])
@pytest.mark.parametrize("nozzle", ["0.2", "0.4", "0.6", "0.8"])
def test_usual_values_are_goal_defaults_even_when_shape_changes_the_plan(goal, nozzle):
    p = Profile.from_dict({"goal": goal, "nozzle": nozzle})
    base = goal_plan(p)
    stats = {"wall_typical_mm": 0.8, "wall_min_mm": 0.8, "wall_upper_mm": 0.8,
             "infill_volume_share": 0, "infill_volume_cm3": 0}
    rows = {s["key"]: s for s in recommend_settings(p, [], stats)}
    for key, field in [("layer_height", "layer_height"), ("wall_loops", "walls"),
                       ("top_shell_layers", "top"), ("bottom_shell_layers", "bottom"),
                       ("sparse_infill_pattern", "pattern")]:
        assert rows[key]["usual"] == str(base[field])
    assert rows["sparse_infill_density"]["usual"] == f"{base['infill']}%"
    assert rows["wall_loops"]["value"] == ("2" if nozzle == "0.2" else "1")
    assert rows["wall_loops"]["usual"] != rows["wall_loops"]["value"]
    assert rows["enable_support"]["usual"] is None  # the goal does not define the slicer's support setting
    json.dumps(list(rows.values()), allow_nan=False)


SETTINGS = {"layer_height": "0.2", "wall_loops": "3", "sparse_infill_density": "15%",
            "sparse_infill_pattern": "gyroid", "top_shell_layers": "4", "bottom_shell_layers": "3",
            "enable_support": "1", "support_type": "tree(auto)", "ironing_type": "topmost"}
PRUSA = {"layer_height": "layer_height", "wall_loops": "perimeters", "sparse_infill_density": "fill_density",
         "sparse_infill_pattern": "fill_pattern", "top_shell_layers": "top_solid_layers",
         "bottom_shell_layers": "bottom_solid_layers", "enable_support": "support_material",
         "support_type": "support_material_style", "ironing_type": "ironing_type"}


@pytest.mark.parametrize("omitted", SETTINGS)
def test_explicitly_omitted_setting_is_absent_in_both_3mf_configs(omitted):
    api.load(to_binary_stl(Mesh(extrude(box(0, 0, 20, 20), 0, 10))), "block.stl")
    selected = {k: v for k, v in SETTINGS.items() if k != omitted}
    archive = zipfile.ZipFile(io.BytesIO(api.export([0, 0, 1], {}, selected)))
    orca = {m.attrib["key"] for m in ET.fromstring(archive.read("Metadata/model_settings.config")).iter("metadata")}
    prusa = {m.attrib["key"] for m in ET.fromstring(archive.read("Metadata/Slic3r_PE_model.config")).iter("metadata")}
    assert omitted not in orca
    assert PRUSA[omitted] not in prusa
    assert (set(SETTINGS) - {omitted}) <= orca
    if omitted == "ironing_type":
        assert "ironing" not in prusa
    if omitted == "enable_support":
        assert "support_material_auto" not in prusa


def test_export_with_every_row_unticked_does_not_fall_back_to_recommendations():
    api.load(to_binary_stl(Mesh(extrude(box(0, 0, 20, 20), 0, 10))), "block.stl")
    archive = zipfile.ZipFile(io.BytesIO(api.export([0, 0, 1], {}, {})))
    for name in ("Metadata/model_settings.config", "Metadata/Slic3r_PE_model.config"):
        keys = {m.attrib["key"] for m in ET.fromstring(archive.read(name)).iter("metadata")}
        assert not keys & (set(SETTINGS) | set(PRUSA.values()))
