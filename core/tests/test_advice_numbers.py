"""Tests for T-435: exact-number CAD advice on the part for every design fix."""
from functools import lru_cache
import json
import re
from pathlib import Path

import numpy as np
import pytest

from fdmcore import api, reports

SAMPLES = Path(__file__).resolve().parents[2] / "web" / "samples"
REQUIRED_KEYS = {"id", "rule", "text", "now", "target", "unit", "point", "fixes", "edit_id"}


def _load(name):
    api.load((SAMPLES / f"{name}.stl").read_bytes(), name)
    return api._session.mesh


@lru_cache(maxsize=None)
def _sample_data(name):
    mesh = _load(name)
    lo, hi = mesh.bounds()
    checked = api.check([0, 0, 1], {}, mode="both")
    designed = api.design([0, 0, 1], {}, findings=checked["findings"])
    return lo, hi, checked, designed


def _numbers_in(text):
    return [float(m) for m in re.findall(r"\d+(?:\.\d+)?", text)]


@pytest.mark.parametrize("name", ["test_block", "feature_test", "enclosure"])
def test_advice_has_number_unit_and_point_inside_bbox(name):
    lo, hi, _, r = _sample_data(name)
    advice = r["advice"]
    assert advice, f"expected advice for {name}"
    json.dumps(advice)  # must be JSON-friendly

    ids = set()
    for a in advice:
        assert set(a.keys()) == REQUIRED_KEYS
        assert a["id"] not in ids
        ids.add(a["id"])
        assert isinstance(a["target"], (int, float)) and a["target"] > 0
        assert a["now"] is None or isinstance(a["now"], (int, float))
        assert isinstance(a["unit"], str) and a["unit"]
        assert a["unit"] in a["text"]
        assert any(abs(v - a["target"]) < 0.06 for v in _numbers_in(a["text"])), a["text"]

        pt = np.asarray(a["point"], dtype=float)
        assert pt.shape == (3,)
        assert np.all(pt >= lo - 1e-4) and np.all(pt <= hi + 1e-4), (name, a["id"], pt, lo, hi)

    # Internal _advice payload must never leak into returned edits.
    assert all("_advice" not in e for e in r["edits"])


@pytest.mark.parametrize("name", ["test_block", "feature_test", "enclosure"])
def test_edit_and_advice_agree_on_number(name):
    _, _, _, r = _sample_data(name)
    edits = {e["id"]: e for e in r["edits"]}
    by_edit = {a["edit_id"]: a for a in r["advice"] if a["edit_id"]}

    for eid, e in edits.items():
        if e["source"] == "other":
            assert eid not in by_edit
            continue
        assert eid in by_edit, f"active edit {eid} missing advice item"
        a = by_edit[eid]
        edit_nums = _numbers_in(f"{e['title']} {e['description']}")
        assert any(abs(v - a["target"]) < 0.06 for v in edit_nums), (eid, a["target"], e["title"], e["description"])
        if e["source"] == "finding":
            assert a["fixes"] == e["fixes"]


@pytest.mark.parametrize("name", ["test_block", "feature_test", "enclosure"])
def test_every_geometry_finding_gets_advice(name):
    _, _, checked, r = _sample_data(name)
    advice = r["advice"]
    covered_rules = {a["rule"] for a in advice} | {a["fixes"]["rule"] for a in advice if a["fixes"]}

    geom_findings = [f for f in checked["findings"] if any(fx["domain"] == "geometry" for fx in f["fixes"])]
    assert geom_findings, f"expected geometry findings on {name}"
    for f in geom_findings:
        assert f["rule"] in covered_rules, (name, f["rule"], f["title"])


def test_fastener_request_edit_and_advice_agree():
    _load("enclosure")
    holes = [f for f in api.design([0, 0, 1], {})["features"] if f["type"] == "hole"]
    assert holes
    api.set_fastener(holes[0]["region"], [0, 0, 1], "M3", "bolt")
    r = api.design([0, 0, 1], {})
    fastener_edits = [e for e in r["edits"] if e["id"].startswith("fastener-")]
    assert fastener_edits
    by_edit = {a["edit_id"]: a for a in r["advice"] if a["edit_id"]}
    for e in fastener_edits:
        a = by_edit[e["id"]]
        edit_nums = _numbers_in(f"{e['title']} {e['description']}")
        assert any(abs(v - a["target"]) < 0.06 for v in edit_nums), (e["id"], a["target"], e["title"], e["description"])


def test_reports_include_advice():
    _load("enclosure")
    advice = api.design([0, 0, 1], {})["advice"]
    assert advice
    rep = reports.collect([0, 0, 1], {}, mode="both")
    html = reports.to_html(rep)
    md, _ = reports.to_markdown(rep, "enclosure")
    assert "Design advice for CAD" in html
    assert "Design advice for CAD" in md
    assert advice[0]["text"] in html
    assert advice[0]["text"] in md
