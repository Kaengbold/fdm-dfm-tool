"""Apply every suggested design edit across all sample and corpus parts (T-463).

For every sample (``web/samples/*.stl``) and corpus part (``core/tests/corpus/*.stl``), skipping
any ``*_80k`` heavy mesh, this suite:
- fetches ``api.design`` edits for the best orientation (``api.load`` -> ``api.orientations`` ->
  ``api.check`` -> ``api.design``),
- applies each edit alone and all edits together via ``booleans.apply_edits``,
- checks watertightness (closed manifold + clean ``mesh_health``), volume direction and rough
  magnitude against the edit mesh, unchanged outer bounding box unless declared, unchanged
  separate piece count, and that the edit's target finding is gone or smaller on re-check.

Known open engine bugs are marked ``xfail(strict=True)`` with a clear reason so each flips red
(XPASS) as soon as its fix merges.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("manifold3d")

from fdmcore import api
from fdmcore.booleans import apply_edits, inspect_mesh
from fdmcore.mesh import Mesh, mesh_health, to_binary_stl

ROOT = Path(__file__).resolve().parents[2]
PARTS = [
    p
    for p in (
        sorted((ROOT / "web" / "samples").glob("*.stl"))
        + sorted((Path(__file__).parent / "corpus").glob("*.stl"))
    )
    if not p.stem.endswith("_80k")
]
PART_BY_NAME = {p.stem: p for p in PARTS}

BBOX_TOL_MM = 0.02
# Cutters and overlapping additions deliberately extend past the surface they touch so boolean
# operations fuse cleanly (teardrops include the empty hole cylinder; foot chamfers overhang the
# outer bottom edge), so the part volume changes by a fraction of the edit mesh's own volume.
MIN_VOL_RATIO = 0.05
MAX_VOL_RATIO = 1.05

# feature_test oriented "On its side" was option 0 in the 2026-11 audit (before T-405 re-ranked
# options) and is the case where 6 of 8 ledge fills silently grow the outer Y bound by 0.15 mm
# (T-436) and where a sacrificial layer edit is exercised.
FEATURE_TEST_SIDE_UP = (0.0, -1.0, 0.0)


@dataclass(frozen=True)
class PartCase:
    name: str
    up: tuple[float, float, float]
    mesh: Mesh
    stats: dict
    bounds: tuple[np.ndarray, np.ndarray]
    findings: list[dict]
    edits: list[dict]


@functools.lru_cache(maxsize=None)
def _load_case(path: Path, up: tuple[float, float, float] | None = None) -> PartCase:
    """Load ``path`` and compute its check findings and design edits once per worker."""
    api.load(path.read_bytes(), path.stem)
    mesh = api._require().mesh
    chosen_up = tuple(float(x) for x in (up if up is not None else api.orientations()["orientations"][0]["up"]))
    findings = api.check(list(chosen_up))["findings"]
    edits = api.design(list(chosen_up), findings=findings)["edits"]
    return PartCase(
        name=path.stem,
        up=chosen_up,
        mesh=mesh,
        stats=inspect_mesh(mesh),
        bounds=mesh.bounds(),
        findings=findings,
        edits=edits,
    )


@functools.lru_cache(maxsize=None)
def _recheck_single(part_name: str, up: tuple[float, float, float] | None, edit_id: str) -> tuple[PartCase, dict, list[dict]]:
    """Apply one edit alone and re-check the resulting mesh (cached per worker)."""
    case = _load_case(PART_BY_NAME[part_name], up)
    edit = next((e for e in case.edits if e["id"] == edit_id), None)
    assert edit is not None, f"{part_name} ({case.up}) has no edit {edit_id!r}; got {[e['id'] for e in case.edits]}"
    result = apply_edits(case.mesh, [edit])
    after_findings = _recheck(result, case.up, f"{part_name}_{edit_id}")
    return case, edit, after_findings


def _recheck(mesh: Mesh, up: tuple[float, float, float], name: str) -> list[dict]:
    api.load(to_binary_stl(mesh), name)
    return api.check(list(up))["findings"]


def _edit_volumes(edit: dict) -> tuple[float, float]:
    """Total (added_piece_volume, cut_piece_volume) of an edit's own closed pieces."""
    ops = edit.get("piece_ops") or [edit.get("op", "cut")] * len(edit["pieces"])
    tris = np.frombuffer(edit["mesh"], "<f4").reshape(-1, 3, 3)
    add_v, cut_v = 0.0, 0.0
    start = 0
    for n, piece_op in zip(edit["pieces"], ops):
        vol = abs(Mesh(tris[start:start + n].astype(np.float64)).volume())
        if piece_op == "add":
            add_v += vol
        else:
            cut_v += vol
        start += n
    return add_v, cut_v


def _may_change_outer_size(edit: dict) -> bool:
    """Whether an edit says it grows the part's outer size (``grows_mm``, added by T-436)."""
    return any(edit.get("grows_mm") or [])


def _assert_watertight_and_pieces(result: Mesh, base_pieces: int, label: str) -> dict:
    info = inspect_mesh(result)
    assert info["closed"], f"{label}: result is not watertight ({info['status']})"
    assert info["volume"] > 0, f"{label}: non-positive volume ({info['volume']})"
    health = mesh_health(result)
    assert health["boundary_edges"] == 0, f"{label}: {health['boundary_edges']} boundary edges"
    assert health["non_manifold_edges"] == 0, f"{label}: {health['non_manifold_edges']} non-manifold edges"
    assert health["flipped_faces"] == 0, f"{label}: {health['flipped_faces']} flipped faces"
    assert info["pieces"] == base_pieces, f"{label}: piece count changed {base_pieces} -> {info['pieces']}"
    return info


def _assert_volume_moved(result_vol: float, base_vol: float, edits: list[dict], label: str) -> None:
    dv = result_vol - base_vol
    add_v = sum(_edit_volumes(e)[0] for e in edits)
    cut_v = sum(_edit_volumes(e)[1] for e in edits)
    if len(edits) == 1:
        if add_v > 0 and cut_v == 0:
            assert dv > 0, f"{label}: add edit did not increase volume (dv={dv:.4f}, edit={add_v:.4f})"
            assert MIN_VOL_RATIO * add_v <= dv <= MAX_VOL_RATIO * add_v + 1e-3, (
                f"{label}: add volume delta {dv:.4f} outside [{MIN_VOL_RATIO * add_v:.4f}, {MAX_VOL_RATIO * add_v:.4f}]"
            )
        elif cut_v > 0 and add_v == 0:
            assert dv < 0, f"{label}: cut edit did not decrease volume (dv={dv:.4f}, edit={cut_v:.4f})"
            assert MIN_VOL_RATIO * cut_v <= -dv <= MAX_VOL_RATIO * cut_v + 1e-3, (
                f"{label}: cut volume delta {-dv:.4f} outside [{MIN_VOL_RATIO * cut_v:.4f}, {MAX_VOL_RATIO * cut_v:.4f}]"
            )
        else:
            assert -MAX_VOL_RATIO * cut_v - 1e-3 <= dv <= MAX_VOL_RATIO * add_v + 1e-3, (
                f"{label}: mixed volume delta {dv:.4f} outside [-{cut_v:.4f}, +{add_v:.4f}]"
            )
            assert abs(dv) > 0, f"{label}: edit had no net volume effect"
    else:
        if add_v == 0:
            assert -MAX_VOL_RATIO * cut_v - 1e-3 <= dv < 0, (
                f"{label}: combined cuts gave dv={dv:.4f} (total cut={cut_v:.4f})"
            )
        elif cut_v == 0:
            assert 0 < dv <= MAX_VOL_RATIO * add_v + 1e-3, (
                f"{label}: combined adds gave dv={dv:.4f} (total add={add_v:.4f})"
            )
        else:
            assert -MAX_VOL_RATIO * cut_v - 1e-3 <= dv <= MAX_VOL_RATIO * add_v + 1e-3, (
                f"{label}: combined edits gave dv={dv:.4f} outside [-{cut_v:.4f}, +{add_v:.4f}]"
            )
            assert abs(dv) > 0, f"{label}: combined edits had no net volume effect"


def _assert_outer_size_unchanged(
    result: Mesh, base_bounds: tuple[np.ndarray, np.ndarray], edits: list[dict], label: str
) -> None:
    if any(_may_change_outer_size(e) for e in edits):
        return
    lo0, hi0 = base_bounds
    lo1, hi1 = result.bounds()
    grow = np.maximum(lo0 - lo1, 0.0) + np.maximum(hi1 - hi0, 0.0)
    shrink = np.maximum(lo1 - lo0, 0.0) + np.maximum(hi0 - hi1, 0.0)
    assert np.all(grow <= BBOX_TOL_MM) and np.all(shrink <= BBOX_TOL_MM), (
        f"{label}: outer bounding box changed (grow={np.round(grow, 4).tolist()} mm, "
        f"shrink={np.round(shrink, 4).tolist()} mm)"
    )


def _bbox_max_span(findings: list[dict]) -> float:
    total = 0.0
    for f in findings:
        bbox = f.get("bbox")
        if bbox:
            lo, hi = np.asarray(bbox[0], float), np.asarray(bbox[1], float)
            total += float(np.max(hi - lo))
    return total


def _finding_reduced(rule: str, before: list[dict], after: list[dict]) -> bool:
    """True when ``rule`` is gone, has fewer instances, or shrank in its measured extent."""
    if len(after) < len(before):
        return True
    if len(after) > len(before):
        return False
    if rule == "warping":
        pts0 = sum(len((f.get("metrics") or {}).get("points", [])) for f in before)
        pts1 = sum(len((f.get("metrics") or {}).get("points", [])) for f in after)
        return pts1 < pts0
    if rule == "support_marks":
        a0 = sum(float((f.get("metrics") or {}).get("area", 0.0)) for f in before)
        a1 = sum(float((f.get("metrics") or {}).get("area", 0.0)) for f in after)
        return a1 < a0 - 1.0
    if rule == "sideways_hole_oval":
        # Crossing holes (hole_block) are split into two cylindrical regions; a single teardrop
        # fixes half the hole, shrinking the finding's span along the hole axis.
        s0, s1 = _bbox_max_span(before), _bbox_max_span(after)
        return s0 > 0 and s1 < 0.8 * s0
    if rule in ("too_thin_to_print", "single_line_wall"):
        w0 = min((float((f.get("metrics") or {}).get("width", 0.0)) for f in before), default=0.0)
        w1 = min((float((f.get("metrics") or {}).get("width", 0.0)) for f in after), default=0.0)
        return w1 > w0 + 0.05
    return False


def test_parts_found():
    assert len(PARTS) >= 15, [p.name for p in PARTS]
    assert all(not p.stem.endswith("_80k") for p in PARTS)


@pytest.mark.parametrize("path", PARTS, ids=lambda p: p.stem)
def test_part_edits_alone_and_together(path: Path):
    """Every edit on the best orientation applies alone and all together cleanly, and the
    combined result removes or shrinks every non-elephant_foot target finding.

    ``elephant_foot`` is verified separately (strict xfail below) so its known check bug does not
    hide regressions in geometry or other findings on the same part.
    """
    case = _load_case(path)
    assert case.stats["closed"], f"{case.name}: input STL is not watertight ({case.stats['status']})"
    assert case.edits, f"{case.name}: expected at least one design edit"

    for edit in case.edits:
        label = f"{case.name}::{edit['id']} ({edit['rule']})"
        result = apply_edits(case.mesh, [edit])
        info = _assert_watertight_and_pieces(result, case.stats["pieces"], label)
        _assert_volume_moved(info["volume"], case.stats["volume"], [edit], label)
        _assert_outer_size_unchanged(result, case.bounds, [edit], label)

    all_label = f"{case.name}::all({len(case.edits)})"
    combined = apply_edits(case.mesh, case.edits)
    info = _assert_watertight_and_pieces(combined, case.stats["pieces"], all_label)
    _assert_volume_moved(info["volume"], case.stats["volume"], case.edits, all_label)
    _assert_outer_size_unchanged(combined, case.bounds, case.edits, all_label)

    fixed_rules = sorted({
        e["fixes"]["rule"]
        for e in case.edits
        if e.get("source") == "finding" and e.get("fixes") and e["fixes"]["rule"] != "elephant_foot"
    })
    if fixed_rules:
        after_findings = _recheck(combined, case.up, f"{case.name}_all")
        for rule in fixed_rules:
            before = [f for f in case.findings if f["rule"] == rule]
            after = [f for f in after_findings if f["rule"] == rule]
            assert _finding_reduced(rule, before, after), (
                f"{case.name}: applying all edits did not remove or shrink {rule!r} "
                f"(before={len(before)} {[f['detail'] for f in before]}, "
                f"after={len(after)} {[f['detail'] for f in after]})"
            )


@pytest.mark.parametrize(
    "part_name",
    ["gear", "detail_plate", "knob"],
    ids=lambda n: f"{n}-foot-chamfer",
)
@pytest.mark.xfail(
    strict=True,
    reason="T-487: elephant_foot is flagged for any first-layer hole, chamfered or not",
)
def test_foot_chamfer_reduces_elephant_foot(part_name: str):
    """Applying foot-chamfer should clear the elephant_foot finding it claims to fix."""
    case, _, after_findings = _recheck_single(part_name, None, "foot-chamfer")
    before = [f for f in case.findings if f["rule"] == "elephant_foot"]
    after = [f for f in after_findings if f["rule"] == "elephant_foot"]
    assert before, f"{part_name}: expected elephant_foot finding before edit"
    assert _finding_reduced("elephant_foot", before, after), (
        f"{part_name}::foot-chamfer did not clear elephant_foot (before={len(before)}, after={len(after)})"
    )


def test_feature_test_side_edits_apply_cleanly():
    """feature_test on its side ([0,-1,0]) exercises 8 ledge fills and a sacrificial layer."""
    case = _load_case(PART_BY_NAME["feature_test"], FEATURE_TEST_SIDE_UP)
    ledges = [e for e in case.edits if e["id"].startswith("ledge-")]
    assert len(ledges) >= 6, f"expected >= 6 ledge fills on feature_test [0,-1,0], got {[e['id'] for e in ledges]}"

    for edit in case.edits:
        label = f"feature_test-side::{edit['id']} ({edit['rule']})"
        result = apply_edits(case.mesh, [edit])
        info = _assert_watertight_and_pieces(result, case.stats["pieces"], label)
        _assert_volume_moved(info["volume"], case.stats["volume"], [edit], label)

    combined = apply_edits(case.mesh, case.edits)
    info = _assert_watertight_and_pieces(combined, case.stats["pieces"], "feature_test-side::all")
    _assert_volume_moved(info["volume"], case.stats["volume"], case.edits, "feature_test-side::all")


@pytest.mark.parametrize(
    "edit_id, expected_fail",
    [
        *[
            pytest.param(
                f"ledge-{i}",
                True,
                id=f"feature_test-side-ledge-{i}",
                marks=pytest.mark.xfail(
                    strict=True,
                    reason="T-436: ledge fill on feature_test [0,-1,0] grows outer Y by 0.15 mm (open PR #165)",
                ),
            )
            for i in range(6)
        ],
        pytest.param("ledge-6", False, id="feature_test-side-ledge-6"),
        pytest.param("ledge-7", False, id="feature_test-side-ledge-7"),
    ],
)
def test_feature_test_side_ledge_fills_keep_outer_size(edit_id: str, expected_fail: bool):
    """Ledge fills must stay inside the part's outer bounding box (T-436)."""
    case = _load_case(PART_BY_NAME["feature_test"], FEATURE_TEST_SIDE_UP)
    edit = next((e for e in case.edits if e["id"] == edit_id), None)
    if edit is None:
        if expected_fail:
            # Return so strict xfail turns red (XPASS) if T-436 drops/renumbers this fill.
            return
        pytest.fail(f"feature_test [0,-1,0] has no edit {edit_id!r}")
    result = apply_edits(case.mesh, [edit])
    _assert_outer_size_unchanged(result, case.bounds, [edit], f"feature_test-side::{edit_id}")


SINGLE_RECHECK_CASES = [
    pytest.param("l_bracket", None, "teardrop-8", id="l_bracket-teardrop-8"),
    pytest.param("feature_test", None, "teardrop-61", id="feature_test-teardrop-61"),
    pytest.param("hole_block", None, "teardrop-6", id="hole_block-teardrop-6"),
    pytest.param("tray", None, "chamfer-0", id="tray-chamfer-0"),
    pytest.param("drawer_handle", None, "chamfer-0", id="drawer_handle-chamfer-0"),
    pytest.param("feature_test", None, "ledge-0", id="feature_test-ledge-0"),
    pytest.param("feature_test", FEATURE_TEST_SIDE_UP, "ledge-6", id="feature_test-side-ledge-6"),
    pytest.param(
        "feature_test",
        FEATURE_TEST_SIDE_UP,
        "ledge-0",
        id="feature_test-side-ledge-0",
        marks=pytest.mark.xfail(
            strict=True,
            reason="T-436: ledge-0 protrudes 0.15 mm below the bed on feature_test [0,-1,0], lifting the part and increasing support_marks area",
        ),
    ),
    pytest.param("test_block", None, "wall-1", id="test_block-wall-1"),
    pytest.param("feature_test", None, "wall-1", id="feature_test-wall-1"),
    pytest.param("feature_test", FEATURE_TEST_SIDE_UP, "sacrificial-0", id="feature_test-side-sacrificial-0"),
]


@pytest.mark.parametrize("part_name, up, edit_id", SINGLE_RECHECK_CASES)
def test_single_edit_reduces_its_finding(part_name: str, up: tuple[float, float, float] | None, edit_id: str):
    """Capped sample across every finding-fixing edit kind: applying one edit reduces its finding."""
    case, edit, after_findings = _recheck_single(part_name, up, edit_id)
    assert edit.get("fixes"), f"{part_name}::{edit_id} is not linked to a finding"
    rule = edit["fixes"]["rule"]
    before = [f for f in case.findings if f["rule"] == rule]
    after = [f for f in after_findings if f["rule"] == rule]
    assert before, f"{part_name}::{edit_id}: expected finding {rule!r} before edit"
    assert _finding_reduced(rule, before, after), (
        f"{part_name}::{edit_id} did not remove or shrink {rule!r} "
        f"(before={len(before)} {[f['detail'] for f in before]}, "
        f"after={len(after)} {[f['detail'] for f in after]})"
    )


@pytest.mark.parametrize(
    "part_name, edit_id",
    [
        pytest.param("l_bracket", "teardrop-8", id="l_bracket-teardrop-8"),
        pytest.param("feature_test", "teardrop-61", id="feature_test-teardrop-61"),
    ],
)
def test_teardrop_introduces_no_gap_fuses(part_name: str, edit_id: str):
    """Cutting a teardrop peak must not introduce a narrow gap_fuses failure at its apex."""
    case, _, after_findings = _recheck_single(part_name, None, edit_id)
    before_n = sum(1 for f in case.findings if f["rule"] == "gap_fuses")
    after_n = sum(1 for f in after_findings if f["rule"] == "gap_fuses")
    assert after_n <= before_n, (
        f"{part_name}::{edit_id} introduced {after_n - before_n} new gap_fuses finding(s): "
        f"{[f['detail'] for f in after_findings if f['rule'] == 'gap_fuses']}"
    )
