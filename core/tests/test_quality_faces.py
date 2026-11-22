"""The Quality goal's faces people see (T-563, plan §14, decision 10): a first guess (the outer faces, on the convex
hull) that keeps support off them when ranking orientations, replaced by the show faces the user marks."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from fdmcore import api
from fdmcore.orientation import evaluate

ROOT = Path(__file__).resolve().parents[2]
SAMPLES = ROOT / "web" / "samples"
PROFILE = {"printer": "bambu_p1s", "material": "pla"}


def _load(name: str):
    api.load((SAMPLES / f"{name}.stl").read_bytes(), name)
    return {r["id"]: r for r in api.regions()["regions"]}


def test_guess_keeps_outer_faces_and_leaves_holes_and_insides_out():
    regions = _load("hole_block")
    seen = set(api.visible_faces()["regions"])
    assert all((r["kind"] == "flat") == (i in seen) for i, r in regions.items()), "the block's sides are seen, its hole walls aren't"
    regions = _load("enclosure")
    out = api.visible_faces()
    seen = set(out["regions"])
    assert not any(regions[i]["kind"] in ("hole", "cylinder") for i in seen)
    hidden_flat = [i for i, r in regions.items() if r["kind"] == "flat" and i not in seen]
    assert hidden_flat, "the box's inside walls are hidden"
    assert 0.2 < out["share"] < 0.9
    assert api.dispatch("visible_faces", "{}") == out


def test_support_on_seen_faces_ranks_an_orientation_lower():
    _load("phone_stand")
    settings = api.orientation_settings(api.Profile.from_dict(PROFILE))
    mark = api._pose_marks({"clean": "visible"})
    assert api._pose_marks(None) is None and api._pose_marks({"clean": []}) is None
    supported, clean = evaluate(api._require().mesh, [0, -1, 0], settings), evaluate(api._require().mesh, [0, 0, 1], settings)
    before = supported.score
    mark([supported, clean], settings)
    assert supported.terms["faces"] > 0 and supported.score > before
    assert clean.terms.get("faces", 0.0) < supported.terms["faces"]


def test_optimize_and_check_take_the_faces(monkeypatch):
    _load("phone_stand")
    calls = []
    real = api._face_penalty
    monkeypatch.setattr(api, "_face_penalty", lambda *a: calls.append(a[1].up) or real(*a))
    out = api.optimize("quality", None, "fast", {"printer": "bambu_p1s"}, faces={"clean": "visible"})
    assert out["options"] and calls, "every candidate orientation is scored against the seen faces"
    # The guess adds no finding of its own (support_marks already names the marks); a marked show face does.
    rules = lambda faces: [f for f in api.check([0, -1, 0], PROFILE, faces=faces)["findings"] if f.get("metrics", {}).get("clean_face")]
    assert rules({"clean": "visible"}) == []
    seen = api.visible_faces()["regions"]
    marked = [rules({"clean": [r]}) for r in seen]
    assert any(m and m[0]["severity"] == "problem" for m in marked)


def test_ui_sends_the_guess_to_the_optimizer_and_only_marks_to_the_checks():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    tmp = Path(tempfile.mkdtemp())
    (tmp / "args.mjs").write_text((ROOT / "web" / "steps" / "args.js").read_text(encoding="utf-8"), encoding="utf-8")
    script = (f'import * as A from "{(tmp / "args.mjs").as_uri()}";\n'
              "const q = { goal: 'quality', showFaces: [] }, m = { goal: 'quality', showFaces: [4, 2] }, b = { goal: 'balanced', showFaces: [4] };\n"
              "console.log(JSON.stringify([A.facesArg(q), A.facesArg(m), A.facesArg(b), A.markedFaces(q), A.markedFaces(m), A.markedFaces(b), A.optimizeArgs(q).faces]));")
    proc = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True)
    shutil.rmtree(tmp, ignore_errors=True)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == [{"clean": "visible"}, {"clean": [4, 2]}, None, None, {"clean": [4, 2]}, None, {"clean": "visible"}]
