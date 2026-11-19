"""No thickening of a part that is strong enough; no silent outer-size growth (T-436)."""

import json
from pathlib import Path

import numpy as np
import pytest

from fdmcore import api
from fdmcore.mesh import Mesh, to_binary_stl

ROOT = Path(__file__).resolve().parents[2]
SAMPLES = ROOT / "web" / "samples"
PARTS = sorted(SAMPLES.glob("*.stl")) + sorted((Path(__file__).parent / "corpus").glob("*.stl"))


def _bbox(mesh: Mesh) -> tuple[np.ndarray, np.ndarray]:
    pts = mesh.triangles.reshape(-1, 3)
    return pts.min(axis=0), pts.max(axis=0)


def _l_bracket_loads(kg: float) -> list[dict]:
    """Hold the back plate of l_bracket and push down on the horizontal shelf."""
    regs = api.regions()["regions"]
    back = max((r for r in regs if r["normal"][0] < -0.9), key=lambda r: r["area_mm2"])
    shelf = max((r for r in regs if r["normal"][2] > 0.9), key=lambda r: r["area_mm2"])
    return [{"region": back["id"], "kind": "fixed"},
            {"region": shelf["id"], "kind": "force", "kg": kg, "direction": "push"}]


def test_fine_part_gets_no_thickening_or_stress_chamfer():
    """A part whose strength verdict is 'fine' must not be offered weak-spot thickening or stress chamfers."""
    api.load((SAMPLES / "l_bracket.stl").read_bytes(), "l_bracket")
    up = api.orientations()["orientations"][0]["up"]

    fine = api.strength(up, _l_bracket_loads(1.0))
    assert fine["verdict"] == "fine", fine["safety_factor"]
    fine_ids = {e["id"] for e in api.design(up)["edits"]}
    assert "thicken-weak" not in fine_ids
    assert "stress-chamfer" not in fine_ids

    # Preserved across engine restart (session_state / restore_state).
    saved = json.loads(json.dumps(api.dispatch("session_state")))
    assert saved["strength_verdict"] == "fine"
    api.load((SAMPLES / "l_bracket.stl").read_bytes(), "l_bracket")
    api.dispatch("restore_state", json.dumps({"state": saved}))
    restored_ids = {e["id"] for e in api.design(up)["edits"]}
    assert "thicken-weak" not in restored_ids
    assert "stress-chamfer" not in restored_ids

    # With a heavy enough load (verdict != "fine"), both strength design edits ARE offered.
    weak = api.strength(up, _l_bracket_loads(15.0))
    assert weak["verdict"] != "fine", weak["safety_factor"]
    weak_edits = {e["id"]: e for e in api.design(up)["edits"]}
    assert "thicken-weak" in weak_edits
    assert "stress-chamfer" in weak_edits


def test_growing_edit_reports_grows_mm_and_plain_sentence():
    """Any edit whose added geometry exceeds the part's bounding box carries grows_mm and states it in plain words."""
    api.load((SAMPLES / "l_bracket.stl").read_bytes(), "l_bracket")
    up = api.orientations()["orientations"][0]["up"]
    weak = api.strength(up, _l_bracket_loads(15.0))
    assert weak["verdict"] != "fine"

    edits = {e["id"]: e for e in api.design(up)["edits"]}
    thicken = edits["thicken-weak"]
    assert "grows_mm" in thicken
    grows = thicken["grows_mm"]
    assert len(grows) == 3 and all(isinstance(v, float) for v in grows)
    assert max(grows) > 0.5
    # JSON-friendly (no numpy scalars).
    json.dumps(grows)
    assert "Outer size grows by " in thicken["description"]
    assert ": check what it fits into." in thicken["description"]
    for val, ax in zip(grows, ("X", "Y", "Z")):
        if val > 0.05:
            assert f"in {ax}" in thicken["description"]
        else:
            assert f"in {ax}" not in thicken["description"]

    # Cuts and non-growing additions (the inside-corner stress round on l_bracket) do not claim growth.
    assert "grows_mm" not in edits["stress-chamfer"]
    assert "Outer size grows" not in edits["stress-chamfer"]["description"]
    for e in edits.values():
        if e["op"] == "cut":
            assert "grows_mm" not in e
            assert "Outer size grows" not in e["description"]


def test_feature_test_ledge_fills_never_change_bounding_box():
    """None of the ledge fills on feature_test (especially the 8 on its side) may grow the bounding box."""
    api.load((SAMPLES / "feature_test.stl").read_bytes(), "feature_test")
    part = api._require().mesh
    lo0, hi0 = _bbox(part)
    dims0 = hi0 - lo0

    total_ledges = 0
    side_ledges = 0
    for opt in api.orientations()["orientations"]:
        ledges = [e for e in api.design(opt["up"])["edits"] if e["id"].startswith("ledge-")]
        total_ledges += len(ledges)
        if abs(opt["up"][1]) > 0.9:
            side_ledges = max(side_ledges, len(ledges))
        for e in ledges:
            assert "grows_mm" not in e, (opt["up"], e["id"], e.get("grows_mm"))
            assert "Outer size grows" not in e["description"]
            pts = np.frombuffer(e["mesh"], "<f4").reshape(-1, 3)
            assert np.all(pts.min(axis=0) >= lo0 - 1e-3), (opt["up"], e["id"], pts.min(axis=0), lo0)
            assert np.all(pts.max(axis=0) <= hi0 + 1e-3), (opt["up"], e["id"], pts.max(axis=0), hi0)

    assert total_ledges >= 8 and side_ledges >= 6

    manifold3d = pytest.importorskip("manifold3d")
    del manifold3d
    from fdmcore.booleans import apply_edits

    for opt in api.orientations()["orientations"]:
        ledges = [e for e in api.design(opt["up"])["edits"] if e["id"].startswith("ledge-")]
        for e in ledges:
            out = apply_edits(part, [e])
            olo, ohi = _bbox(out)
            assert np.allclose(ohi - olo, dims0, atol=1e-3), (opt["up"], e["id"], ohi - olo, dims0)
            assert abs(out.volume()) > abs(part.volume())


def test_all_sample_and_corpus_ledge_fills_stay_in_bbox():
    """Across every sample and corpus part and orientation option, no ledge fill grows the bounding box."""
    for path in PARTS:
        api.load(path.read_bytes(), path.stem)
        part = api._require().mesh
        lo0, hi0 = _bbox(part)
        for opt in api.orientations()["orientations"]:
            for e in api.design(opt["up"])["edits"]:
                if not e["id"].startswith("ledge-"):
                    continue
                assert "grows_mm" not in e, (path.stem, opt["up"], e["id"], e.get("grows_mm"))
                pts = np.frombuffer(e["mesh"], "<f4").reshape(-1, 3)
                assert np.all(pts.min(axis=0) >= lo0 - 1e-3), (path.stem, opt["up"], e["id"])
                assert np.all(pts.max(axis=0) <= hi0 + 1e-3), (path.stem, opt["up"], e["id"])


def test_ledge_fill_clips_xy_to_thin_wall_and_narrow_ceiling():
    """A ledge on a wall thinner than OVERLAP (0.12 mm) and a thin top flange (0.12 mm) clips cleanly inside the part's bbox."""
    manifold3d = pytest.importorskip("manifold3d")
    from fdmcore.booleans import apply_edits

    cube = manifold3d.Manifold.cube
    # Thin vertical wall (0.12 mm in X, < OVERLAP=0.2) supporting a 4 mm ledge at z=5 with a 0.12 mm roof.
    m = (cube((0.12, 20.0, 5.0)) + cube((4.12, 20.0, 0.12)).translate((0.0, 0.0, 5.0))).to_mesh()
    mesh = Mesh(m.vert_properties[m.tri_verts.reshape(-1)].reshape(-1, 3, 3).astype(float))
    api.load(to_binary_stl(mesh), "thin_ledge")
    part = api._require().mesh
    lo0, hi0 = _bbox(part)

    ledges = [e for e in api.design([0, 0, 1])["edits"] if e["id"].startswith("ledge-")]
    assert len(ledges) == 1
    pts = np.frombuffer(ledges[0]["mesh"], "<f4").reshape(-1, 3)
    assert np.all(pts.min(axis=0) >= lo0 - 1e-3), (pts.min(axis=0), lo0)
    assert np.all(pts.max(axis=0) <= hi0 + 1e-3), (pts.max(axis=0), hi0)
    assert "grows_mm" not in ledges[0]

    out = apply_edits(part, ledges)
    olo, ohi = _bbox(out)
    assert np.allclose(ohi - olo, hi0 - lo0, atol=1e-3)
    assert abs(out.volume()) > abs(part.volume())
