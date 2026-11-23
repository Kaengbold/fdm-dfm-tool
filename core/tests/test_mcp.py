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


def test_orcaslicer_gcode_stats():
    """OrcaSlicer's print time, layers and filament from its G-code (profile matching: test_slicer_cli)."""
    from fdmcore.slicer_cli import parse_gcode

    g = "; HEADER_BLOCK_START\n; model printing time: 30m 9s; total estimated time: 1h 5m 53s\n; total layer number: 298\n" \
        "; FEATURE: Support\n; filament used [mm] = 4571.60\n; filament used [cm3] = 11.00\n"
    assert parse_gcode(g) == {"print_time_min": 65.9, "model_time_min": 30.1, "layers": 298, "filament_mm": 4571.6,
                              "filament_cm3": 11.0, "support": True}


def test_every_api_method_has_a_tool():
    """Agents can do everything the web app does: a new api.METHODS entry needs a tool in API_TOOLS."""
    import asyncio

    from fdmcore import api

    tools = {t.name for t in asyncio.run(m.server.list_tools())}
    assert set(api.METHODS) - set(m.API_TOOLS) - m.APP_ONLY == set(), "add a tool and an API_TOOLS entry"
    assert set(m.API_TOOLS) - set(api.METHODS) == set()
    assert set(m.API_TOOLS.values()) - tools == set()


def test_creep_calibration_tool_saves_the_print_and_instructions(tmp_path):
    path = tmp_path / "creep.stl"
    result = m.calibration_part("creep", str(path))
    assert load_stl(path.read_bytes()).face_count > 0
    assert "500 g" in result["how"]
    assert result["creep_test"]["stress_mpa"] == 9.19


def test_more_tools(tmp_path):
    m.load_part(str(SAMPLES / "l_bracket.stl"))
    up = [0.0, 0.0, 1.0]
    assert m.check_orientation(up)["up"] == pytest.approx(up)
    assert "lift_ratio" in m.check_warping(up, material="abs")
    assert len(m.compare_nozzles(up)) >= 2
    assert m.heat_curve("petg", 70)["temps_c"]
    heat = m.box_heat(3.0)
    assert heat["verdict"] in {"fine", "warm", "too hot"} and "face_temp_c" not in heat
    corner = m.box_heat(3.0, point=heat["hottest_point"])
    assert corner["source"] != heat["source"] and corner["wall_max_c"] >= heat["inside_c"]
    cal = m.calibration_part("fit", str(tmp_path / "fit.stl"))
    assert (tmp_path / "fit.stl").stat().st_size > 0 and "peg" in cal["how"]
    assert m.expert_settings()["sections"]
    saved, image = m.render_image("bed", up, str(tmp_path / "bed.png"), width=160, height=120)
    assert (tmp_path / "bed.png").read_bytes() == image.data and "bed.png" in saved

    faces = sorted((f for f in m.list_faces() if f["kind"] == "flat"), key=lambda f: -f["area_mm2"])
    held, pushed = faces[0], next(f for f in faces[1:] if abs(f["normal"][2]) < 0.5)
    loads = [{"region": held["id"], "kind": "fixed"}, {"region": pushed["id"], "kind": "force", "kg": 2}]
    zone = m.add_zone(up, region=pushed["id"])
    base = m.check_strength(up, loads, zones=[zone])["safety_factor"]
    same = m.strength_variant(up, loads, str(SAMPLES / "l_bracket.stl"), zones=[zone])
    assert same["safety_factor"] == pytest.approx(base, rel=0.05) and abs(same["added_g"]) < 0.5

    box = m.place_box(up, "add_solid", region=pushed["id"])
    before = m.load_part(str(SAMPLES / "l_bracket.stl"))["volume_mm3"]
    grown = m.apply_blocks(up, [box], str(tmp_path / "grown.stl"))
    assert grown["volume_mm3"] > before
    keep = m.place_box(up, "keep_clear", region=pushed["id"])
    assert "edits" in m.suggest_design_changes(up, keep_clear=[keep])

    snapped = m.snap_model(str(tmp_path / "fit.stl"), pushed["centroid"], pushed["normal"], str(tmp_path / "joined.stl"))
    assert snapped["volume_mm3"] > grown["volume_mm3"] and snapped["placed"]


def test_list_faces_and_edges():
    m.load_part(str(SAMPLES / "l_bracket.stl"))
    faces = m.list_faces()
    holes = [f for f in faces if f["kind"] == "hole"]
    assert len(holes) == 2
    assert sorted(round(h["hole_diameter"], 1) for h in holes) == [6.0, 8.0]

    edges = m.list_edges()
    assert edges["feature_edges"] and edges["concave_edges"]
    corner = edges["concave_edges"][0]
    assert corner["length_mm"] == pytest.approx(30.0, abs=1.0)
    assert len(corner["points"]) >= 2 and corner["radius"] > 0

    pushed = next(
        f for f in sorted(faces, key=lambda f: -f["area_mm2"])
        if f["kind"] == "flat" and abs(f["normal"][2]) < 0.5
    )
    res = m.check_strength(
        [0.0, 0.0, 1.0],
        [
            {"points": corner["points"], "radius": corner["radius"], "kind": "fixed"},
            {"region": pushed["id"], "kind": "force", "kg": 1.0},
        ],
    )
    assert res["safety_factor"] > 0

    m.load_part(str(SAMPLES / "enclosure.stl"))
    kinds = {f["kind"] for f in m.list_faces()}
    assert {"flat", "cylinder", "hole"} <= kinds


def test_file_access_limits(tmp_path, monkeypatch):
    mine = tmp_path / "mine.stl"
    mine.write_bytes(b"the user's own file")
    with pytest.raises(ValueError, match="already exists"):
        m._out(str(mine), (".stl",))
    out = m._out(str(tmp_path / "new.stl"), (".stl",))
    out.write_bytes(b"x")
    assert m._out(str(out), (".stl",)) == out  # the server's own output can be replaced again
    with pytest.raises(ValueError, match="must end in"):
        m._in(str(tmp_path / "notes.txt"), m.PARTS)
    monkeypatch.setenv("FDM_DFM_FOLDER", str(tmp_path / "jail"))
    with pytest.raises(ValueError, match="only use files inside"):
        m.load_part(str(SAMPLES / "l_bracket.stl"))
    with pytest.raises(ValueError, match="only use files inside"):
        m._out(str(tmp_path / "jail" / ".." / "escape.stl"), (".stl",))
    assert m._in(str(tmp_path / "jail" / "part.stl"), m.PARTS).parent == (tmp_path / "jail").resolve()
