from pathlib import Path
import xml.etree.ElementTree as ET

from fdmcore.profile import load_data


ROOT = Path(__file__).resolve().parents[2]
PICTURES = ROOT / "web" / "img" / "symptoms"


def test_symptom_pictures_match_symptoms():
    ids = set(load_data("symptoms")["symptoms"])

    assert {path.stem for path in PICTURES.glob("*.svg")} == ids
    for symptom_id in ids:
        path = PICTURES / f"{symptom_id}.svg"
        assert path.is_file()
        content = path.read_text(encoding="utf-8")
        root = ET.fromstring(content)
        assert root.attrib.get("viewBox") == "0 0 96 72"
        assert root.find("{http://www.w3.org/2000/svg}title") is not None
        assert "<script" not in content.lower()
        assert 'href="http' not in content.lower()
        assert path.stat().st_size < 3072
