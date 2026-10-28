from pathlib import Path

import pytest

pytest.importorskip("mcp")
pytest.importorskip("manifold3d")

from fdmcore import mcp_server as m  # noqa: E402
from fdmcore.mesh import load_stl  # noqa: E402

SAMPLES = Path(__file__).resolve().parents[2] / "web" / "samples"


def test_agent_workflow(tmp_path):
    assert "pla" in m.list_presets()["materials"]
    info = m.load_part(str(SAMPLES / "l_bracket.stl"))
    assert "triangles" not in info  # binary payloads are stripped for agents
    prof = {"printer": "prusa_mk4s", "material": "abs"}
    up = m.find_orientations(**prof)["orientations"][0]["up"]
    rules = {f["rule"] for f in m.check_print(up, **prof)["findings"]}
    assert "warping" in rules

    edits = [e["id"] for e in m.suggest_design_changes(up, **prof)["edits"]]
    out = tmp_path / "fixed.stl"
    applied = m.apply_design_changes(up, edits, str(out), **prof)
    assert applied["volume_mm3"] < info["volume_mm3"]
    assert load_stl(out.read_bytes()).face_count > 0
    # Chamfered corners lift less, but a 60 mm ABS base on an open printer can still warp.
    warp = next(f for f in m.check_print(up, **prof)["findings"] if f["rule"] == "warping")
    assert warp["metrics"]["points"] == []

    path = m.export_3mf(up, str(tmp_path / "part.3mf"), **prof, settings={"wall_loops": "5"})
    assert Path(path).stat().st_size > 0
    assert m.get_report(up, **prof).startswith("# Print check")
