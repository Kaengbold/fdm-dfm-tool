"""T-573: the mating part heat map (api.clearance_map) and the hold brush's engine format (web/steps/args.js)."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import pytest
from shapely.geometry import Point, box

from fdmcore import api
from fdmcore.additions import extrude
from fdmcore.mesh import Mesh, to_binary_stl

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def plate():
    """A 30 × 30 × 10 mm plate with a Ø8.4 hole at (15, 15)."""
    api.load(to_binary_stl(Mesh(extrude(box(0, 0, 30, 30).difference(Point(15, 15).buffer(4.2, quad_segs=64)), 0, 10))), "plate")


def _peg(r: float) -> bytes:
    return to_binary_stl(Mesh(extrude(Point(15, 15).buffer(r, quad_segs=64), -5, 15)))


def test_gap_painted_on_the_part(plate):
    r = api.clearance_map(_peg(4.1))
    assert r["verdict"] == "snug" and r["gap_mm"] == pytest.approx(0.1, abs=0.01)  # clearance()'s answer comes along
    tris = np.frombuffer(r["field_triangles"], np.float32).reshape(-1, 3)
    gap = np.frombuffer(r["vertex_gap_mm"], np.float32)
    assert len(gap) == len(tris)
    wall = np.abs(np.hypot(tris[:, 0] - 15, tris[:, 1] - 15) - 4.2) < 0.05
    assert np.allclose(gap[wall], 0.1, atol=0.02)  # the hole wall: 0.1 mm per side all round
    assert gap[~wall].min() > 0.5  # the rest is far from the peg
    at = np.array(r["tight_at"])
    assert np.hypot(at[0] - 15, at[1] - 15) == pytest.approx(4.2, abs=0.05) and at[2] == pytest.approx(10)  # at the mouth
    mate = np.frombuffer(r["mate_triangles"], np.float32).reshape(-1, 9)
    assert len(mate) > 100 and r["range_mm"] == 1.0


def test_overlap_shows_negative(plate):
    r = api.clearance_map(_peg(4.3))
    assert r["verdict"] == "overlap" and r["tight_mm"] == pytest.approx(-0.1, abs=0.02)


def test_brush_hold_goes_to_the_engine_as_points():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    tmp = Path(tempfile.mkdtemp())
    (tmp / "args.mjs").write_text((ROOT / "web" / "steps" / "args.js").read_text(encoding="utf-8"), encoding="utf-8")
    script = (f'import * as A from "{(tmp / "args.mjs").as_uri()}";\n'
              "const painted = { kind: 'brush', region: 3, edge: null, type: 'clamped', points: [[1, 2, 10], [3, 2, 10]], normals: [[0, 0, 1], [0, 0, 1]], radius: 4 };\n"
              "const face = { kind: 'face', region: 3, edge: null, type: 'resting' };\n"
              "console.log(JSON.stringify([A.holdToEngine(painted), A.holdToEngine(face)]));")
    proc = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True)
    shutil.rmtree(tmp, ignore_errors=True)
    assert proc.returncode == 0, proc.stderr
    painted, face = json.loads(proc.stdout)
    assert painted == {"points": [[1, 2, 10], [3, 2, 10]], "radius": 4, "kind": "fixed", "hold": "clamped"}
    assert face == {"region": 3, "kind": "fixed", "hold": "resting"}
