"""STEP face ids agree between the importer and OpenCascade, and an inside corner leads to its B-rep edge (T-527).

The B-rep edits (T-528 …) name CAD faces by the id the STEP importer (occt-import-js) gave each triangle, and apply
them in OpenCascade (replicad's build). tools/check_step_faces.mjs reads every test STEP both ways (the L-bracket
fixture and core/tests/corpus_step/) and compares each body's faces by area and centre. Needs Node and the two
packages in tools/node_modules (see that script); skipped without them, like tools/pyodide_check.mjs."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from fdmcore import api

ROOT = Path(__file__).resolve().parents[2]
MODULES = ROOT / "tools" / "node_modules"


@pytest.fixture(scope="module")
def checked(tmp_path_factory):
    node = shutil.which("node")
    if not node or not all((MODULES / p).exists() for p in ("replicad-opencascadejs", "occt-import-js")):
        pytest.skip("needs node and tools/node_modules (replicad-opencascadejs, occt-import-js)")
    dump = tmp_path_factory.mktemp("step")
    proc = subprocess.run([node, str(ROOT / "tools" / "check_step_faces.mjs"), "--dump", str(dump)],
                          capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout.strip().splitlines()[-1]), dump


def test_face_order_matches_on_every_test_step(checked):
    out, _ = checked
    names = [f["name"] for f in out["files"]]
    assert "l_bracket.step" in names and len(names) >= 4, names
    for f in out["files"]:
        solids = [c for c in f["checks"] if c["solid"] is not None]
        assert len(solids) == f["solids"], f  # every solid is one of the importer's bodies (some files add loose surfaces)
        assert all(c["ok"] for c in solids), f
    assert sum(c["solid"] is not None for f in out["files"] for c in f["checks"]) >= 16  # the two-body file counts twice
    assert out["corner"]["driftCaught"]  # a wrong order would be noticed (then the edit falls back to the mesh)


def test_the_brackets_inside_corner_is_one_shared_edge(checked):
    corner = checked[0]["corner"]
    assert len(corner["edges"]) == 1 and corner["apart"] == 0
    edge = corner["edges"][0]
    assert edge["length"] == pytest.approx(30) and np.allclose(edge["centre"], [-25, 0, 5], atol=1e-6)


def test_the_engine_names_the_corner_by_its_b_rep_faces(checked):
    """Loaded with the importer's face ids, the engine's inside-corner polyline there says which two CAD faces it
    lies between: the same two OpenCascade shares the edge of."""
    out, dump = checked
    api.load((dump / "l_bracket.stl").read_bytes(), "l_bracket", json.loads((dump / "l_bracket_faces.json").read_text()))
    r = api.regions()
    corner = [m for m in r["concave_edges"]["meta"] if abs(m["midpoint"][0] + 25) < 0.5 and abs(m["midpoint"][2] - 5) < 0.5]
    assert corner and sorted(corner[0]["faces"]) == sorted(out["corner"]["faces"]), corner
    assert all("face" in info for info in r["regions"])
    api.load((dump / "l_bracket.stl").read_bytes(), "l_bracket")  # an STL has no CAD faces to name
    assert all("faces" not in m for m in api.regions()["concave_edges"]["meta"])
