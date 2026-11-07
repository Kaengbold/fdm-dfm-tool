import json
from pathlib import Path

import pytest

from fdmcore import api, cli, mcp_server as m, project

PART = Path(__file__).resolve().parents[2] / "web" / "samples" / "l_bracket.stl"


def test_round_trip_keeps_face_ids(tmp_path):
    m.load_part(str(PART))
    faces = sorted((f for f in m.list_faces() if f["kind"] == "flat"), key=lambda f: -f["area_mm2"])
    loads = [{"region": faces[0]["id"], "kind": "fixed"}, {"region": faces[1]["id"], "kind": "force", "kg": 2}]
    up = [0.0, 0.0, 1.0]
    path = tmp_path / "bracket.json"
    m.save_project(str(path), up, loads=loads, faces={"clean": [faces[2]["id"]]}, settings={"wall_loops": "4"}, material="petg")
    before = m.check_strength(up, loads, material="petg")["safety_factor"]
    opened = m.open_project(str(path))
    assert opened["up"] == up and opened["loads"] == loads and opened["profile"]["material"] == "petg"
    assert opened["settings"] == {"wall_loops": "4"} and opened["part"]["name"] == PART.name
    assert m.check_strength(opened["up"], opened["loads"], material="petg")["safety_factor"] == pytest.approx(before)


def test_cli_reads_a_project(tmp_path, capsys):
    api.load(PART.read_bytes(), PART.name)
    stl = Path(PART).read_bytes()
    path = tmp_path / "p.json"
    path.write_text(project.dump("bracket.stl", stl, profile={"material": "abs"}, up=[0, 1, 0], settings={"wall_loops": "5"}))
    assert cli.main(["export", str(path), "-o", str(tmp_path / "p.3mf")]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["up"] == [0, 1, 0] and out["settings"]["wall_loops"] == "5"
    assert cli.main(["check", str(path), "--up", "0,0,1", "--material", "pla"]) == 0  # flags win over the project
    assert json.loads(capsys.readouterr().out)["up"] == pytest.approx([0, 0, 1])


def test_not_a_project():
    with pytest.raises(ValueError, match="project"):
        project.read('{"format": "other"}')
    with pytest.raises(ValueError, match="newer"):
        project.read(json.dumps({"format": project.FORMAT, "version": 99, "part": {}}))
