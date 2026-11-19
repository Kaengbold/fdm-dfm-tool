import json
import shutil
import subprocess
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
    import zipfile

    api.load(PART.read_bytes(), PART.name)
    stl = Path(PART).read_bytes()
    path = tmp_path / "p.json"
    path.write_text(project.dump("bracket.stl", stl, profile={"material": "abs"}, up=[0, 1, 0],
                                 settings={"wall_loops": "5"}, excluded_settings=["top_shell_layers"]))
    assert cli.main(["export", str(path), "-o", str(tmp_path / "p.3mf")]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["up"] == [0, 1, 0] and out["settings"]["wall_loops"] == "5"
    assert "top_shell_layers" not in out["settings"]
    with zipfile.ZipFile(tmp_path / "p.3mf") as archive:
        assert 'key="top_shell_layers"' not in archive.read("Metadata/model_settings.config").decode()
        assert 'key="top_solid_layers"' not in archive.read("Metadata/Slic3r_PE_model.config").decode()
    assert cli.main(["check", str(path), "--up", "0,0,1", "--material", "pla"]) == 0  # flags win over the project
    assert json.loads(capsys.readouterr().out)["up"] == pytest.approx([0, 0, 1])


def test_round_trip_keeps_suggestion_choices():
    edits = {"gusset-12": True, "fit-4": False, "teardrop-8": False}
    boxes = {}
    for region, use in [(12, True), (8, False)]:
        key = json.dumps(["support", region, None, None], separators=(",", ":"))
        boxes[key] = {"kind": "support", "from": {"region": region}, "use": use,
                      "lo": [0, 0, 0], "hi": [2, 3, 4], "corners_print": [[0, 0, 0]],
                      "what": "flat face", "why": "This face may droop."}
    stl = PART.read_bytes()
    text = project.dump(PART.name, stl, edit_selections=edits, suggested_boxes=boxes)
    assert json.loads(text)["version"] == project.VERSION == 1
    opened = project.read(text)
    assert opened["stl"] == stl
    assert opened["edit_selections"] == edits
    assert opened["suggested_boxes"] == boxes
    # Unticks must survive even when there are no ticks at all.
    opened = project.read(project.dump(PART.name, stl, edit_selections={"fit-4": False}))
    assert opened["edit_selections"] == {"fit-4": False}


def test_old_project_has_no_suggestion_overrides():
    text = project.dump(PART.name, PART.read_bytes(), holes={"fits": {"4": {"partner_mm": 8, "fit": "snug"}}})
    opened = project.read(text)
    assert "edit_selections" not in opened and "suggested_boxes" not in opened
    assert opened["holes"]["fits"]["4"]["partner_mm"] == 8


@pytest.mark.parametrize("choices", [{}, {"load_settings": {"values": {"wall_loops": "2"}, "met": True, "kept": False}}, {"excluded_settings": ["wall_loops", "layer_height"]},
                                       {"edit_selections": {"fit-4": False, "teardrop-8": True},
                                        "suggested_boxes": {'["support",4,null,null]': {
                                            "kind": "support", "from": {"region": 4}, "use": False}}}])
def test_browser_and_python_project_round_trip(choices):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to exercise the browser project reader/writer")
    # The browser module has no DOM dependencies: run the real reader and writer on Python's JSON.
    script = """
      import fs from 'node:fs';
      const source = fs.readFileSync(process.argv[1]).toString('base64');
      const { readProject, writeProject } = await import('data:text/javascript;base64,' + source);
      process.stdout.write(writeProject(readProject(fs.readFileSync(0, 'utf8'))));
    """
    text = project.dump(PART.name, PART.read_bytes(), face_ids=[4, 8], **choices)
    result = subprocess.run([node, "--input-type=module", "-e", script, str(PART.parents[1] / "project.js")],
                            input=text, text=True, capture_output=True, check=True, timeout=30)
    assert project.read(result.stdout) == project.read(text)


def test_not_a_project():
    with pytest.raises(ValueError, match="project"):
        project.read('{"format": "other"}')
    with pytest.raises(ValueError, match="newer"):
        project.read(json.dumps({"format": project.FORMAT, "version": 99, "part": {}}))
