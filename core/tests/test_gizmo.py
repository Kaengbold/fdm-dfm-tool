"""Direction gizmo math (T-542, plan research/redesign-2026-11/PLAN-COMBINED.md §8 point 2), tested under Node.

gizmo.js is copied to .mjs with its three.js import stubbed: the pure helpers (snapping, ring axes, tilt) don't use it."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NODE = shutil.which("node")


def run_js(body: str):
    if not NODE:
        pytest.skip("node not installed")
    tmp = Path(tempfile.mkdtemp())
    src = (ROOT / "web" / "viewport" / "gizmo.js").read_text(encoding="utf-8").replace('import * as THREE from "three";', "const THREE = {};")
    (tmp / "gizmo.mjs").write_text(src, encoding="utf-8")
    script = f'import * as G from "{(tmp / "gizmo.mjs").as_uri()}";\nconst out = (() => {{ {body} }})();\nconsole.log(JSON.stringify(out));'
    proc = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True)
    shutil.rmtree(tmp, ignore_errors=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_snapping_is_15_degrees_unless_free():
    out = run_js("return [G.snapAngle(22), G.snapAngle(-8), G.snapAngle(7.4), G.snapAngle(22, 15, true), G.snapAngle(31, 5)];")
    assert out == [15, -15, 0, 22, 30]


def test_ring_axes_are_perpendicular_for_any_base():
    out = run_js("""
      const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
      return [[0, 0, 1], [0, 0, -1], [1, 0, 0], [0.3, -0.5, 0.81]].map((b) => {
        const { u, v } = G.ringAxes(b), n = Math.hypot(...b), bn = b.map((x) => x / n);
        return [dot(u, bn), dot(v, bn), dot(u, v), Math.hypot(...u), Math.hypot(...v)];
      });
    """)
    for row in out:
        assert row[:3] == pytest.approx([0, 0, 0], abs=1e-9) and row[3:] == pytest.approx([1, 1])


def test_tilt_turns_the_arrow_by_the_ring_angle():
    out = run_js("""
      const base = G.baseDirection("push", [0, 0, 1]);  // into a top face: straight down
      const t15 = G.tiltedDirection(base, 15, 0), t90 = G.tiltedDirection(base, 90, 0), both = G.tiltedDirection(base, 30, 45);
      return { base, a15: G.angleBetween(base, t15), a90: G.angleBetween(base, t90), unit: Math.hypot(...both),
               pull: G.baseDirection("pull", [0, 2, 0]), x: G.baseDirection("-x", [0, 0, 1]), zero: G.tiltedDirection(base, 0, 0) };
    """)
    assert out["base"] == pytest.approx([0, 0, -1])
    assert out["a15"] == pytest.approx(15) and out["a90"] == pytest.approx(90)
    assert out["unit"] == pytest.approx(1)
    assert out["pull"] == pytest.approx([0, 1, 0]) and out["x"] == pytest.approx([-1, 0, 0])
    assert out["zero"] == pytest.approx(out["base"])
