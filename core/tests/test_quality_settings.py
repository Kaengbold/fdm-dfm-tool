"""Quality settings and calibration (T-572): the looks settings for the Quality goal, the temperature tower and its
G-code pass, the ironing test's per-step flows (research/quality_settings.md)."""

from __future__ import annotations

import io
import re
import zipfile

import numpy as np
import pytest

from fdmcore import api
from fdmcore.gcode import add_temperatures
from fdmcore.profile import Profile

LOOKS = ("outer_wall_speed", "outer_wall_acceleration", "wall_sequence", "seam_position")


def _settings(profile: dict) -> dict:
    return {s["key"]: s["value"] for s in api.check([0, 0, 1], profile)["settings"]}


@pytest.fixture(scope="module")
def plate():
    """A 60 × 60 × 10 mm block: a big flat top, so ironing comes up."""
    from shapely.geometry import box

    from fdmcore.additions import extrude
    from fdmcore.mesh import Mesh, to_binary_stl

    api.load(to_binary_stl(Mesh(extrude(box(0, 0, 60, 60), 0, 10))), "plate.stl")
    yield
    api.custom_presets()


def test_looks_settings_only_for_quality(plate):
    s = _settings({"printer": "bambu_p1s", "looks": True})
    # Bambu's High Quality profiles: 60 mm/s (0.3 × a fast printer's 200), 2000 mm/s²; inner-outer-inner; aligned seam.
    assert s["outer_wall_speed"] == "60" and s["outer_wall_acceleration"] == "2000"
    assert s["wall_sequence"] == "inner-outer-inner wall" and s["seam_position"] == "aligned"
    assert s["ironing_type"] == "top" and s["ironing_flow"] == "10%" and s["ironing_spacing"] == "0.1"
    plain = _settings({"printer": "bambu_p1s"})
    assert not any(k in plain for k in LOOKS) and plain["ironing_type"] == "topmost" and "ironing_flow" not in plain


def test_looks_never_speeds_up_a_slower_printer(plate):
    s = _settings({"printer": "prusa_mini", "looks": True})
    assert s["outer_wall_speed"] == "42"  # 0.3 × a medium printer's 140
    assert "outer_wall_acceleration" not in s  # its own default is often lower already


def test_measured_ironing_flow_wins(plate):
    api.custom_presets(materials={"pla_mine": {"based_on": "pla", "ironing_flow_pct": 14, "nozzle_temp_c": 210}})
    s = _settings({"printer": "bambu_p1s", "material": "my_pla_mine"})
    assert s["ironing_flow"] == "14%"  # also outside Quality, whenever it irons
    assert Profile.from_dict({"material": "my_pla_mine"}).material["nozzle_temp_c"] == 210
    with pytest.raises(ValueError, match="ironing_flow_pct"):
        api.custom_presets(materials={"bad": {"based_on": "pla", "ironing_flow_pct": 90}})
    api.custom_presets()


def test_looks_settings_go_into_the_3mf(plate):
    data = api.export([0, 0, 1], {"printer": "bambu_p1s", "looks": True})
    z = zipfile.ZipFile(io.BytesIO(data))
    config = z.read("Metadata/model_settings.config").decode()
    assert 'key="wall_sequence" value="inner-outer-inner wall"' in config and 'key="outer_wall_speed" value="60"' in config
    prusa = zipfile.ZipFile(io.BytesIO(api.export([0, 0, 1], {"printer": "prusa_mini", "looks": True}, slicer="prusa")))
    text = "".join(prusa.read(n).decode("utf-8", "replace") for n in prusa.namelist() if n.endswith(".config"))
    assert "external_perimeter_speed" in text and 'seam_position' in text and "ironing_flowrate" in text


def test_temperature_tower_part():
    r = api.calibration_part("temp", {"material": "pla"})
    assert r["temps"] == [230, 225, 220, 215, 210, 205]  # PLA's 220 °C +10 … −15, hottest at the bottom
    assert api.calibration_part("temp", {"material": "petg"})["temps"][2] == 255
    tris = np.frombuffer(r["stl"][84:], dtype=np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")]))["v"]
    assert tris[..., 2].max() == pytest.approx(1 + 6 * 8)
    assert tris[..., 1].min() < -0.3  # the temperatures stand out of the front face


def _sliced(layers: list[float], second_layer_temp: bool = True) -> str:
    """A minimal sliced G-code: start code, a layer mark and height per layer, a slicer's second-layer temperature,
    and the end code turning the heater off."""
    out = ["M104 S220", "M109 S220", "G28"]
    for i, z in enumerate(layers):
        out += [";LAYER_CHANGE", f";Z:{z:g}", ";HEIGHT:0.2", f"G1 Z{z:g} F600"]
        if i == 1 and second_layer_temp:
            out.append("M104 S220 ; second layer")
        out.append("G1 X10 Y10 E0.5 F1800")
    out += ["M104 S0", "M140 S0"]
    return "\n".join(out)


def test_temperatures_go_in_from_the_first_layer_of_each_block():
    layers = [round(0.2 * k, 2) for k in range(1, 26)]  # 0.2 … 5.0
    text, where = add_temperatures(_sliced(layers), [(1.0, 230), (3.0, 225)])
    assert [w["z"] for w in where] == [1.2, 3.2]  # the layer ending at 1.0 still belongs below
    lines = text.splitlines()
    temps = [(i, int(re.search(r"S(\d+)", l).group(1))) for i, l in enumerate(lines) if re.match(r"M10[49]\b", l)]
    assert temps[0][1] == 220 and temps[1][1] == 220  # the start code is the slicer's
    assert [t for _, t in temps[2:]] == [220, 230, 225, 0]  # second layer untouched (below the first band), then the bands, heater off
    text, _ = add_temperatures(_sliced(layers), [(0.1, 240)])  # a band from the first layer: the slicer's change follows it
    assert "M104 S220 ; second layer" not in text and text.count("M104 S240") >= 2
    with pytest.raises(ValueError, match="ends below"):
        add_temperatures(_sliced(layers), [(1.0, 230), (9.0, 225)])
    with pytest.raises(ValueError, match="layer marks"):
        add_temperatures("G28\nG1 X1 E1\n", [(1.0, 230)])


def test_tower_gcode_api():
    r = api.tuning_gcode("temp", _sliced([round(0.2 * k, 2) for k in range(1, 250)]).encode(), {"material": "pla"})
    assert r["temps"] == [230, 225, 220, 215, 210, 205] and [b["temp_c"] for b in r["bands"]] == r["temps"]
    assert r["data"].decode().rstrip().endswith("M140 S0") and "M104 S0" in r["data"].decode()


def test_ironing_test_sets_each_steps_flow():
    r = api.calibration_part("ironing")
    assert r["flows"] == [6, 9, 12, 15, 18]
    z = zipfile.ZipFile(io.BytesIO(r["3mf"]))
    ranges = z.read("Metadata/layer_config_ranges.xml").decode()
    flows = re.findall(r'opt_key="ironing_flow">(\d+)%<', ranges)
    assert flows == ["18", "15", "12", "9", "6"]  # lowest step first
    prusa = z.read("Metadata/Prusa_Slicer_layer_config_ranges.xml").decode()
    assert prusa.count('opt_key="ironing_flowrate"') == 5
    config = z.read("Metadata/model_settings.config").decode()
    assert 'key="ironing_type" value="top"' in config
