"""The Simulation step's engine side (T-560): ``api.print_mesh`` hands the browser OrcaSlicer the part as it prints, and
``load_gcode`` must find that same part again in a G-code sliced from it."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from fdmcore import api

SAMPLES = Path(__file__).resolve().parents[2] / "web" / "samples"


def test_print_mesh_is_the_part_turned_as_it_prints():
    api.load((SAMPLES / "hook.stl").read_bytes(), "hook")
    profile = {"printer": "bambu_p1s", "material": "pla"}
    for up in ([0, 0, 1], [0, -1, 0], [1, 0, 0]):
        out = api.print_mesh(up, profile)
        tri = np.frombuffer(out["triangles"], dtype=np.float32).reshape(-1, 3, 3)
        assert out["count"] == len(tri) == len(api._require().mesh.triangles)
        pts = tri.reshape(-1, 3)
        lo, hi = pts.min(axis=0), pts.max(axis=0)
        assert abs(lo[2]) < 1e-4, "rests on the bed"
        assert np.allclose((lo[:2] + hi[:2]) / 2, 0, atol=1e-3), "centred, the slicer moves it to the bed centre"
        assert np.allclose(sorted(hi - lo), sorted(out["size_mm"]), atol=0.01)
        # The same turn as check(): its rotation maps the part's up direction to +z.
        rot = np.asarray(out["rotation"])
        assert np.allclose(rot @ (np.asarray(up, float) / np.linalg.norm(up)), [0, 0, 1], atol=1e-6)
        assert np.allclose(rot, api.check(up, profile)["rotation"], atol=1e-9)


def test_print_mesh_goes_through_dispatch():
    api.load((SAMPLES / "hook.stl").read_bytes(), "hook")
    out = api.dispatch("print_mesh", '{"up": [0, 0, 1], "profile": {"printer": "bambu_p1s"}}')
    assert isinstance(out["triangles"], bytes) and len(out["triangles"]) == out["count"] * 36
