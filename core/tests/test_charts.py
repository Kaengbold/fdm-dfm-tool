import xml.etree.ElementTree as ET
from pathlib import Path

from fdmcore import api, charts

SAMPLES = Path(__file__).resolve().parents[2] / "web" / "samples"


def parse(svg: str) -> ET.Element:
    root = ET.fromstring(svg)  # well-formed XML
    assert root.tag.endswith("svg") and root.find("{http://www.w3.org/2000/svg}title").text
    return root


def test_charts_from_api_results():
    api.load((SAMPLES / "l_bracket.stl").read_bytes(), "l_bracket.stl")
    up = [0.0, 0.0, 1.0]
    assert "Support needed" in charts.orientations(api.orientations({}, top=3))
    parse(charts.orientations(api.orientations({}, top=3)))
    for svg in charts.nozzles(api.compare_nozzles(up)):
        assert "0.4 mm nozzle" in svg and parse(svg) is not None
    parse(charts.warping(api.warping(up, {"material": "abs"})))
    heat = charts.heat(api.heat_curve({"material": "petg"}, 60))
    assert "60 °C" in heat and parse(heat) is not None
    parse(charts.sweep([10, 20, 40], [1.2, 1.6, 2.3], "infill (%)"))


def test_safety_bars_coloured_by_target():
    svg = charts.orientations({"orientations": [{"label": "A", "safety_factor": 3.0}, {"label": "B", "safety_factor": 0.8}]})
    assert charts.GOOD in svg and charts.BAD in svg and "aim ×2" in svg
    assert "&lt;" in charts.bar_chart(["<b>"], [1], "t")  # labels are escaped
