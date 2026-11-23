"""Exact rounds and chamfers at inside corners of STEP parts (T-534): the engine's round where the strength check
finds the most stress becomes a real fillet (a cylinder of the engine's radius) on the edge the two named B-rep faces
share; where a round would hang over it is a real chamfer; where OpenCascade can't make it (a corpus part, a round
bigger than the pocket it is in) the faceted wedge goes in instead, with a note. Runs web/brep.js under Node
(tools/check_brep_edits.mjs); skipped without Node and tools/node_modules, like test_brep_edits.py."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from fdmcore import api
from test_brep_edits import plain

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).parent
MODULES = ROOT / "tools" / "node_modules"


def corner_edit(name: str, spot, up) -> dict:
    """The engine's round / chamfer at the inside corner nearest ``spot`` on a STEP part (as the importer gives it)."""
    api.load((HERE / "step" / f"{name}.stl").read_bytes(), name, json.loads((HERE / "step" / f"{name}_faces.json").read_text()))
    s = api._require()
    s.weak_spot, s.strength_verdict = (tuple(spot), 1.0), "likely to break"
    return next(e for e in api.design(up)["edits"] if e["id"] == "stress-chamfer")


def apply(step: Path, edits: list[dict], tmp: Path) -> dict:
    node = shutil.which("node")
    if not node or not all((MODULES / p).exists() for p in ("replicad-opencascadejs", "occt-import-js")):
        pytest.skip("needs node and tools/node_modules (replicad-opencascadejs, occt-import-js)")
    path = tmp / "edits.json"
    path.write_text(json.dumps([plain(e) for e in edits]))
    proc = subprocess.run([node, str(ROOT / "tools" / "check_brep_edits.mjs"), str(step), str(path)], capture_output=True, text=True, timeout=900)
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def kinds(surfaces: list[dict], kind: str) -> list[dict]:
    return [s for s in surfaces if s["kind"] == kind]


def test_the_brackets_inside_corner_becomes_a_real_round(tmp_path):
    edit = corner_edit("l_bracket", (-24.0, 0.0, 6.0), [0, 0, 1])
    assert edit["intent"]["op"] == "fillet"
    out = apply(ROOT / "tools" / "fixtures" / "l_bracket.step", [edit], tmp_path)
    r = out["edits"]["stress-chamfer"]["exact"]
    assert [n["how"] for n in r["notes"]] == ["exact"] and r["valid"] and r["faces"] == out["before"]["faces"] + 1
    new = [c["radius"] for c in kinds(r["surfaces"], "cylinder")]
    old = [c["radius"] for c in kinds(out["before"]["surfaces"], "cylinder")]
    (radius,) = [x for x in new if not any(abs(x - y) < 1e-6 for y in old)]
    assert radius == pytest.approx(edit["intent"]["refs"]["edges"][0]["size"], abs=1e-4)  # the engine's radius


def test_upside_down_the_corner_is_a_real_chamfer(tmp_path):
    """Printed upside down the corner faces down: a round would hang over, so it is a 45° chamfer, one flat face."""
    edit = corner_edit("l_bracket", (-24.0, 0.0, 6.0), [0, 0, -1])
    assert edit["intent"]["op"] == "chamfer"
    out = apply(ROOT / "tools" / "fixtures" / "l_bracket.step", [edit], tmp_path)
    r = out["edits"]["stress-chamfer"]["exact"]
    assert [n["how"] for n in r["notes"]] == ["exact"] and r["faces"] == out["before"]["faces"] + 1
    assert len(kinds(r["surfaces"], "plane")) == len(kinds(out["before"]["surfaces"], "plane")) + 1


def test_a_round_too_wide_for_the_pocket_is_made_smaller(tmp_path):
    """The block's pocket: its own corner rounds exactly; one nearly as wide as the pocket can't be made at that size,
    so it is made smaller (still exact) and the note says by how much (T-535)."""
    edit = corner_edit("gen_block", (4.4, 0.0, 12.4), [0, 0, 1])
    assert all(e.get("faces") for e in edit["intent"]["refs"]["edges"])
    wide = json.loads(json.dumps({k: v for k, v in edit.items() if k != "mesh"}))
    wide["mesh"], wide["id"] = edit["mesh"], "too-wide"
    for item in wide["intent"]["refs"]["edges"]:
        item["size"] = 9.0  # the pocket is 10 mm wide
    out = apply(ROOT / "core" / "tests" / "corpus_step" / "gen_block.step", [edit, wide], tmp_path)
    fine, smaller = out["edits"]["stress-chamfer"]["exact"], out["edits"]["too-wide"]["exact"]
    assert [n["how"] for n in fine["notes"]] == ["exact"] and "scale" not in fine["notes"][0] and fine["valid"]
    (note,) = smaller["notes"]
    assert note["how"] == "exact" and note["scale"] in (0.5, 0.25) and smaller["valid"] and smaller["dV"] > 0
