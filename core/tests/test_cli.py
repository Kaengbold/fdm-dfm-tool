import json
from pathlib import Path

import pytest

from fdmcore import cli

PART = str(Path(__file__).resolve().parents[2] / "web" / "samples" / "l_bracket.stl")


def run(capsys, *args) -> dict:
    assert cli.main(list(args)) == 0
    return json.loads(capsys.readouterr().out)


def test_check_faces_strength_report_export(capsys, tmp_path):
    assert "pla" in run(capsys, "presets")["materials"]
    best = run(capsys, "orient", PART, "--top", "2", "--material", "abs")["orientations"][0]["up"]
    check = run(capsys, "check", PART, "--material", "abs")
    assert check["up"] == pytest.approx(best) and any(f["rule"] == "warping" for f in check["findings"])
    assert "face_classes" not in json.dumps(check)  # viewer bytes are dropped

    faces = sorted((f for f in run(capsys, "faces", PART) if f["kind"] == "flat"), key=lambda f: -f["area_mm2"])
    held, pushed = faces[0], next(f for f in faces[1:] if abs(f["normal"][2]) < 0.5)
    loads = tmp_path / "loads.json"
    loads.write_text(json.dumps([{"region": held["id"], "kind": "fixed"}, {"region": pushed["id"], "kind": "force", "kg": 2}]))
    s = run(capsys, "strength", PART, "--up", "0,0,1", "--loads", str(loads))
    assert s["safety_factor"] > 0 and s["verdict"]

    md = tmp_path / "r.md"
    assert run(capsys, "report", PART, "-o", str(md))["saved"] == str(md)
    assert md.read_text(encoding="utf-8").startswith("# Print check")
    out = run(capsys, "export", PART, "-o", str(tmp_path / "p.3mf"), "--settings", '{"wall_loops": "5"}')
    assert out["settings"]["wall_loops"] == "5" and (tmp_path / "p.3mf").stat().st_size > 0


def test_errors_are_one_line(capsys):
    assert cli.main(["strength", PART]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: --loads is required") and err.count("\n") == 1


def test_render(capsys, tmp_path):
    out = tmp_path / "under.png"
    r = run(capsys, "render", PART, "--up", "0,0,1", "--view", "overhangs", "--width", "160", "--height", "120", "-o", str(out))
    assert r["view"] == "overhangs" and out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
