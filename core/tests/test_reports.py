import json
from pathlib import Path

from fdmcore import api, cli, mcp_server, render, reports

PART = Path(__file__).resolve().parents[2] / "web" / "samples" / "l_bracket.stl"
PNG = render.png(render.np.zeros((2, 2, 3), render.np.uint8))


def test_html_and_markdown(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(render, "picture", lambda view, *a, **k: calls.append(view) or PNG)  # pictures are tested in test_render
    api.load(PART.read_bytes(), PART.name)
    up, profile = [0.0, 0.0, 1.0], {"material": "abs"}
    html = reports.write(str(tmp_path / "r.html"), up, profile)
    page = (tmp_path / "r.html").read_text(encoding="utf-8")
    assert html["saved"] == [str(tmp_path / "r.html")] and page.count("data:image/png;base64,") == len(calls)
    assert "<svg" in page and "Warping" in page and "problem" in calls and "warp" in calls
    md = reports.write(str(tmp_path / "r.md"), up, profile)
    text = (tmp_path / "r.md").read_text(encoding="utf-8")
    assert len(md["saved"]) > 3 and all(Path(p).exists() for p in md["saved"])
    assert all(f"]({Path(p).name})" in text for p in md["saved"][1:])  # every saved picture / chart is linked


def test_cli_and_mcp(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(render, "picture", lambda *a, **k: PNG)
    assert cli.main(["report", str(PART), "--up", "0,0,1", "-o", str(tmp_path / "r.html")]) == 0
    assert json.loads(capsys.readouterr().out)["saved"] == [str(tmp_path / "r.html")]
    assert cli.main(["chart", str(PART), "--up", "0,0,1", "--kind", "heat", "--material", "petg", "-o", str(tmp_path / "h.svg")]) == 0
    assert (tmp_path / "h.svg").read_text(encoding="utf-8").startswith("<svg")
    saved = mcp_server.make_chart("nozzles", str(tmp_path / "n.svg"), [0.0, 0.0, 1.0])["saved"]
    assert [Path(p).name for p in saved] == ["n.svg", "n_filament.svg"]
