"""Design edits made on the STEP part's B-rep with exact CAD tools (T-529): the engine's edits for the L-bracket
fixture (with their intents, T-528) go through web/brep.js applyEdits under Node (tools/check_brep_edits.mjs), as
the write-back worker runs them. Exact edits keep the face count near the original (a resized hole stays one
cylinder), the same edit forced onto the mesh path comes out faceted, and one that can't be made exactly falls back
to its pieces. Needs Node and tools/node_modules (see tools/check_step_faces.mjs); skipped without them."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from fdmcore import api
from fdmcore.features import _intent, _prism, _prism_shape
from test_edit_intents import UP, load_bracket

ROOT = Path(__file__).resolve().parents[2]
MODULES = ROOT / "tools" / "node_modules"
STEP = ROOT / "tools" / "fixtures" / "l_bracket.step"


def plain(e: dict) -> dict:
    return {k: (np.frombuffer(v, "<f4").tolist() if k == "mesh" else v) for k, v in e.items() if not k.startswith("_")}


def gusset() -> dict:
    """A rib in the bracket's inside corner, as an edit the user placed: a triangle 10 mm each way, 2 mm thick."""
    outline = [np.array(p, float) for p in ((-25.3, 0, 4.7), (-15, 0, 4.7), (-25.3, 0, 15))]
    half = np.array([0, 1.0, 0])
    return {"id": "rib", "rule": "design_change", "title": "Rib in the corner", "op": "add", "param": None,
            "mesh": _prism(outline, half).astype("<f4").tobytes(), "pieces": [8],
            "intent": _intent("rib", {"thickness_mm": 2.0}, [_prism_shape([p - half for p in outline], 2 * half, add=True)])}


@pytest.fixture(scope="module")
def applied(tmp_path_factory):
    node = shutil.which("node")
    if not node or not all((MODULES / p).exists() for p in ("replicad-opencascadejs", "occt-import-js")):
        pytest.skip("needs node and tools/node_modules (replicad-opencascadejs, occt-import-js)")
    load_bracket()
    hole = next(r for r in api.regions()["regions"] if r.get("hole_diameter") and abs(r["hole_diameter"] - 6) < 0.2)
    api.set_fit(hole["id"], UP, partner_mm=6.5, fit="loose")
    s = api._require()
    s.weak_spot, s.strength_verdict = ((-24.0, 0.0, 6.0), 1.0), "likely to break"
    edits = api.design(UP)["edits"]
    picked = [next(e for e in edits if e["id"].startswith(k)) for k in ("teardrop", "fit-", "stress-chamfer")]
    picked += api.volume_solid(UP, [{"kind": "add_solid", "lo": [0, -10, 5], "hi": [20, 10, 7]},
                                    {"kind": "cut_solid", "lo": [5, -5, 1], "hi": [15, 5, 3]}])
    picked.append(gusset())
    broken = dict(picked[2], id="too-big-round")  # a round far bigger than the part: OpenCascade can't make it, even smaller
    broken["intent"] = json.loads(json.dumps(picked[2]["intent"]))
    for item in broken["intent"]["refs"]["edges"]:
        item["size"] = 600.0
    picked.append(broken)
    path = tmp_path_factory.mktemp("brep") / "edits.json"
    path.write_text(json.dumps([plain(e) for e in picked]))
    proc = subprocess.run([node, str(ROOT / "tools" / "check_brep_edits.mjs"), str(STEP), str(path)],
                          capture_output=True, text=True, timeout=900)
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def by_id(out: dict, prefix: str) -> dict:
    return next(v for k, v in out["edits"].items() if k.startswith(prefix))


@pytest.mark.parametrize("prefix", ["teardrop", "fit-", "solid-0", "solid-1", "rib", "stress-chamfer"])
def test_each_edit_is_made_with_exact_faces(applied, prefix):
    r = by_id(applied, prefix)
    exact, mesh = r["exact"], r["mesh"]
    assert exact["valid"] and [n["how"] for n in exact["notes"]] == ["exact"], exact["notes"]
    assert exact["faces"] <= applied["before"]["faces"] + 7, exact  # a few new faces, not facets
    assert exact["dV"] == pytest.approx(mesh["dV"], rel=0.05, abs=2.0)  # the same change of plastic either way


def test_a_resized_hole_stays_one_cylinder(applied):
    r = by_id(applied, "fit-")
    assert r["exact"]["faces"] == applied["before"]["faces"] and r["exact"]["dV"] < 0
    assert r["mesh"]["faces"] > 3 * applied["before"]["faces"]  # the faceted path: strips round the hole


def test_a_change_that_fails_exactly_falls_back_to_its_pieces(applied):
    r = applied["edits"]["too-big-round"]["exact"]
    (note,) = r["notes"]
    assert note["how"] == "mesh" and note["why"] and r["valid"] and r["dV"] > 0


def test_all_together_write_one_valid_step(applied):
    every = applied["all"]
    assert every["valid"] and {n["how"] for n in every["notes"] if n["id"] != "too-big-round"} == {"exact"}
    assert applied["written"] == {"solids": 1, "faces": every["faces"], "valid": True}
    assert every["meshFaces"] == every["faces"] and every["triangles"] > 0  # shown and re-checked with its face ids

