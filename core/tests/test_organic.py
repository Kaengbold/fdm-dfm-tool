"""CAD part or organic shape (T-408): sculpted shapes get no checks that assume CAD intent."""

from pathlib import Path

import numpy as np
import pytest

from fdmcore import api
from fdmcore.mesh import Mesh, to_binary_stl
from fdmcore.profile import load_data
from fdmcore.regions import planar_share

ROOT = Path(__file__).resolve().parents[2]
CORPUS_PARTS = sorted((ROOT / "core" / "tests" / "corpus").glob("*.stl"))
ORGANIC_PARTS = [p for p in CORPUS_PARTS if p.stem.startswith("bumpy_blob")]
CAD_PARTS = sorted((ROOT / "web" / "samples").glob("*.stl")) + [p for p in CORPUS_PARTS if p not in ORGANIC_PARTS]


def blob(n: int = 120, bumps: bool = True) -> Mesh:
    """A closed bumpy shape with no flat face and no hole: what a sculpt or scan looks like to the checks."""
    lon = np.linspace(0, 2 * np.pi, n, endpoint=False)
    lat = np.linspace(0, np.pi, n + 2)
    az, po = np.meshgrid(lon, lat)
    bump = 0.15 * np.sin(7 * az) * np.sin(5 * po) + 0.05 * np.sin(23 * az + 11 * po) if bumps else 0 * az
    r = 30 * (1 + bump)
    v = r[..., None] * np.stack([np.sin(po) * np.cos(az), np.sin(po) * np.sin(az), np.cos(po)], axis=-1)
    v[..., 2] *= 80 / np.ptp(v[..., 2])
    row, col = np.meshgrid(np.arange(n + 1), np.arange(n), indexing="ij")
    lo, beside = row * n + col, row * n + (col + 1) % n
    faces = np.concatenate([np.stack([lo, lo + n, beside], -1)[1:].reshape(-1, 3),
                            np.stack([beside, lo + n, beside + n], -1)[:-1].reshape(-1, 3)])
    return Mesh(v.reshape(-1, 3)[faces])


@pytest.mark.parametrize("path", CAD_PARTS, ids=lambda p: p.name)
def test_cad_parts_stay_cad(path):
    info = api.load(path.read_bytes(), path.name)
    assert info["kind"] == "cad" and info["cad_checks"]


@pytest.mark.parametrize("path", ORGANIC_PARTS, ids=lambda p: p.name)
def test_organic_corpus_parts_load_as_organic(path):
    info = api.load(path.read_bytes(), path.name)
    assert info["kind"] == "organic" and not info["cad_checks"]


def test_planar_share_separates_shapes():
    below = load_data("thresholds")["organic"]["planar_share_below"]
    assert planar_share(blob()) < 0.1 < below
    assert planar_share(blob(400)) < 0.1  # finely meshed: neighbours within 0.5°, still not in one plane
    assert planar_share(blob(bumps=False)) > 0.95  # a revolved shape is how CAD makes one


def test_step_faces_are_cad():
    mesh = blob()
    info = api.load(to_binary_stl(mesh), "sculpt", face_ids=list(range(mesh.face_count)))
    assert info["kind"] == "cad"


def test_organic_shape_gets_no_cad_findings():
    info = api.load(to_binary_stl(blob()), "sculpt")
    assert info["kind"] == "organic" and not info["cad_checks"]
    cad_rules = set(load_data("thresholds")["organic"]["cad_rules"])
    off = api.check([0, 0, 1], mode="both")
    rules = {f["rule"] for f in off["findings"]}
    assert not rules & cad_rules
    assert off["kind"] == "organic" and not off["cad_checks"]
    assert api._features() == []
    assert all("hole_diameter" not in r for r in api.regions()["regions"])

    # The switch brings them back, for a CAD part that was taken for a sculpt.
    assert api.set_cad_checks(True) == {"kind": "organic", "cad_checks": True}
    on = api.check([0, 0, 1], mode="both")
    assert {f["rule"] for f in on["findings"]} & cad_rules
    assert api._features()
    # Every check that holds for any shape still runs with the CAD checks off.
    assert rules == {f["rule"] for f in on["findings"]} - cad_rules
