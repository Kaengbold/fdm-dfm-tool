"""Problems & fixes step of the new UI (T-561, plan §13): web/steps/fixes.js is pure logic, run under Node here on
real engine output (check, design and regions of the hole block); the page itself is driven by the e2e smoke test
("new UI: problems")."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import pytest

from fdmcore import api

ROOT = Path(__file__).resolve().parents[2]
SAMPLES = ROOT / "web" / "samples"
NODE = shutil.which("node")
PROFILE = {"printer": "bambu_p1s", "material": "pla"}


def run_js(body: str, data: dict):
    """Run ``body`` with F = steps/fixes.js and D = ``data``; returns what it returns (JSON)."""
    if not NODE:
        pytest.skip("node not installed")
    tmp = Path(tempfile.mkdtemp())
    (tmp / "fixes.mjs").write_text((ROOT / "web" / "steps" / "fixes.js").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp / "data.json").write_text(json.dumps(data), encoding="utf-8")
    script = (
        f'import * as F from "{(tmp / "fixes.mjs").as_uri()}";\n'
        'import { readFileSync } from "node:fs";\n'
        f'const D = JSON.parse(readFileSync({json.dumps(str(tmp / "data.json"))}, "utf8"));\n'
        f"const out = await (async () => {{ {body} }})();\n"
        "console.log(JSON.stringify(out));"
    )
    proc = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True)
    shutil.rmtree(tmp, ignore_errors=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _no_bytes(x):
    if isinstance(x, dict):
        return {k: _no_bytes(v) for k, v in x.items() if not isinstance(v, bytes)}
    if isinstance(x, list):
        return [_no_bytes(v) for v in x]
    return x


@pytest.fixture(scope="module")
def hole_block():
    api.load((SAMPLES / "hole_block.stl").read_bytes(), "hole_block")
    check = api.check([0, 0, 1], PROFILE)
    design = api.design([0, 0, 1], PROFILE, findings=check["findings"])
    return {"findings": _no_bytes(check["findings"]), "design": _no_bytes(design), "regions": _no_bytes(api.regions())}


def test_groups_worst_first_and_fixes_slicer_design_process(hole_block):
    out = run_js("""
      const groups = F.groupFindings(D.findings).map((g) => [g.impact, g.findings.length]);
      const hole = D.findings.find((f) => f.rule === 'sideways_hole_oval');
      const open = F.fixesFor(hole, D.design).map((x) => [x.tag, x.editIds?.length ?? 0, Boolean(x.steps), Boolean(x.changesModel)]);
      const locked = F.fixesFor(hole, D.design, { modelLocked: true }).filter((x) => x.tag === 'design' && !x.cad).every((x) => x.changesModel);
      return { groups, open, locked };""", hole_block)
    impacts = [g[0] for g in out["groups"]]
    assert impacts == sorted(impacts, key=["fails", "shape", "surface", "weak", "info"].index)
    assert sum(n for _, n in out["groups"]) == len(hole_block["findings"])
    tags = [x[0] for x in out["open"]]
    assert tags[0] == "slicer" and "design" in tags and tags[-1] == "process", tags
    assert tags == sorted(tags, key=["slicer", "design", "process"].index)
    # A through hole is two hole ends: one "Teardrop" fix carries both edits.
    teardrop = [x for x in out["open"] if x[0] == "design" and x[1]]
    assert teardrop and teardrop[0][1] == 2, out["open"]
    assert out["locked"]


def test_pending_batch_conflicts_and_settings():
    out = run_js("""
      const o = { walls: 3, infill: 0.15, settings: { wall_loops: '3', sparse_infill_density: '15%' } };
      const f1 = { finding_id: 'a' }, f2 = { finding_id: 'b' };
      const p1 = F.pendingItem({ id: 's1', tag: 'slicer', text: 'more walls', settings: { wall_loops: 4 } }, f1, o);
      const p2 = F.pendingItem({ id: 's2', tag: 'slicer', text: 'even more', settings: { wall_loops: '5', sparse_infill_density: '25%' } }, f2, o);
      const p3 = F.pendingItem({ id: 'e1', tag: 'design', text: 'Teardrop', editIds: ['t1'] }, f1, o);
      const clash = F.conflicts([p1, p2, p3]);
      const applied = F.applySettings(o, [p2, p3]);
      return { p1, p3, clash, none: F.conflicts([p1, p3]).length, applied,
        status: [F.statusOf(f2, { option: applied }), F.statusOf(f1, { option: applied, pending: [p3] }), F.statusOf(f1, { option: applied, accepted: ['a'] })] };""", {})
    assert out["p1"]["changes"] == [{"key": "wall_loops", "from": "3", "to": "4"}]
    assert out["p3"] == {"id": "e1", "kind": "design", "findingId": "a", "title": "Teardrop", "editIds": ["t1"]}
    assert out["clash"] == [{"key": "wall_loops", "values": ["4", "5"], "ids": ["s1", "s2"]}] and out["none"] == 0
    a = out["applied"]
    assert a["walls"] == 5 and a["infill"] == pytest.approx(0.25) and a["settings"]["wall_loops"] == "5" and a["fixed"] == ["b"]
    assert out["status"] == ["fixed", "pending", "accepted"]


def test_unattached_bracket_corner_edits_can_go_into_pending_batch():
    api.load((SAMPLES / "l_bracket.stl").read_bytes(), "l_bracket")
    up = [0, 0, 1]
    findings = api.check(up, PROFILE)["findings"]
    assert not any(f["rule"] == "warping" for f in findings)
    design = api.design(up, PROFILE, findings=findings)
    corner_ids = [e["id"] for e in design["edits"] if e["rule"] == "sharp_bed_corners"]
    assert len(corner_ids) == 4
    blocked = {**design["edits"][0], "id": "blocked", "source": "other", "clash": True}
    out = run_js("""
      const fixes = F.optionalImprovements(D, { modelLocked: true });
      return { fixes, pending: F.pendingItem(fixes[0], null, {}) };""",
                 _no_bytes({**design, "edits": [*design["edits"], blocked]}))
    assert sorted(i for fix in out["fixes"] for i in fix["editIds"]) == sorted(corner_ids)
    assert all(fix["changesModel"] for fix in out["fixes"])
    first = out["fixes"][0]
    assert out["pending"] == {"id": first["id"], "kind": "design", "findingId": None,
                              "title": first["text"], "editIds": first["editIds"]}


def test_new_and_fixed_problems():
    before = [{"finding_id": "x1", "rule": "hole", "severity": "warn"}, {"finding_id": "x2", "rule": "hole", "severity": "warn"},
              {"finding_id": "i1", "rule": "note", "severity": "info"}]
    after = [{"finding_id": "x2", "rule": "hole", "severity": "warn"}, {"finding_id": "w9", "rule": "thin_wall", "severity": "problem"},
             {"finding_id": "i2", "rule": "note", "severity": "info"}]
    out = run_js("""return { fresh: F.newProblems(D.before, D.after), fixed: F.fixedCount(D.before, D.after),
      withSetting: F.fixedCount(D.before, D.after, [{ kind: 'setting', findingId: 'x2' }, { kind: 'setting', findingId: 'x2' }]) };""",
                 {"before": before, "after": after})
    assert out == {"fresh": ["w9"], "fixed": 1, "withSetting": 2}


def test_holds_and_loads_carry_over_to_a_changed_model(hole_block):
    """The same model again keeps every face; a model with a face gone drops what was on it."""
    regions = hole_block["regions"]
    top = max(regions["regions"], key=lambda r: r["normal"][2])["id"]
    edge = regions["feature_edges"]["meta"][3]
    items = [{"region": top, "type": "clamped"}, {"region": top, "edge": edge["id"], "edgeKind": "feature"}]
    gone = {**regions, "regions": [r for r in regions["regions"] if r["id"] != top]}
    out = run_js("return [F.carryOver(D.items, D.r, D.r), F.carryOver(D.items.slice(0, 1), D.r, D.gone)];",
                 {"items": items, "r": regions, "gone": gone})
    assert out[0] == {"items": items, "lost": 0}
    assert out[1]["lost"] == 1 and out[1]["items"] == []


def test_a_finding_box_maps_back_to_the_part():
    """check's boxes are in the print frame; turned on its side, the whole part's print box maps back to its own box."""
    api.load((SAMPLES / "l_bracket.stl").read_bytes(), "l_bracket")
    up = [0, -1, 0]
    rot = np.asarray(api.check(up, PROFILE)["rotation"])
    soup = api._require().mesh.triangles.reshape(-1, 3)
    turned = soup @ rot.T
    lo, hi = turned.min(axis=0), turned.max(axis=0)
    base = np.array([(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, lo[2]])
    bbox = [(lo - base).tolist(), (hi - base).tolist()]
    out = run_js("""const soup = Float32Array.from(D.soup);
      const [lo, hi] = F.turnedBounds(soup, D.rot);
      return F.boxToPart(D.bbox, D.rot, lo, hi);""", {"soup": soup.reshape(-1).tolist(), "rot": rot.tolist(), "bbox": bbox})
    corners = np.asarray(out)
    assert np.allclose(corners.min(axis=0), soup.min(axis=0), atol=1e-3)
    assert np.allclose(corners.max(axis=0), soup.max(axis=0), atol=1e-3)
