"""The heat network (T-473): a hand-worked cooling case, and the T-471 fixture part (a block with a thin pin
on top) against the simpler line-by-line check."""

import gzip
import json
import zipfile
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from fdmcore import api, gcode, kinematics, thermal
from fdmcore.profile import Profile, load_data

FIXTURES = Path(__file__).parent / "gcode"
PLA = Profile.from_dict({"material": "pla", "printer": "bambu_p1s"})
PIN_Z = 6.4  # above this only the Ø3 mm pin prints


@pytest.fixture(scope="module")
def orca():
    tp = gcode.parse(gzip.decompress((FIXTURES / "orca.gcode.gz").read_bytes()))
    return tp, kinematics.timing(tp)


@pytest.fixture(scope="module")
def quick(orca):
    return thermal.simulate(*orca, PLA, "quick")


def plate_then_dot(pause_s: float, size: float = 12.0) -> str:
    """A solid square plate, a pause, then one short line on its middle (OrcaSlicer-style)."""
    w, lh = 0.45, 0.2
    e = w * lh / (np.pi * 1.75**2 / 4)
    out = ["M83", "M106 S0", ";LAYER_CHANGE", "G1 Z0.2 F600", "; FEATURE: Internal solid infill"]
    for i, y in enumerate(np.arange(w / 2, size, w)):
        x0, x1 = (0, size) if i % 2 == 0 else (size, 0)
        out += [f"G1 X{x0:.3f} Y{y:.3f} F6000", f"G1 X{x1:.3f} Y{y:.3f} E{size * e:.5f} F3000"]
    m = size / 2
    out += [f"G4 S{pause_s}", ";LAYER_CHANGE", "G1 Z0.4 F600", f"G1 X{m - 0.5} Y{m} F6000", f"G1 X{m + 0.5} Y{m} E{e:.5f} F600"]
    return "\n".join(out)


def stacked_beads(width: float, height: float) -> gcode.Toolpath:
    """Two one-mm lines directly above one another, each inside the same cell."""
    volume = height * (width - height) + np.pi * height**2 / 4
    filament = volume / (np.pi * 1.75**2 / 4)
    text = f"""M83
;LAYER_CHANGE
G1 X0.5 Y0.5 Z{height} F600
; FEATURE: Internal solid infill
G1 X1.5 E{filament:.8f} F600
G4 S20
;LAYER_CHANGE
G1 X0.5 Z{2 * height} F600
G1 X1.5 E{filament:.8f} F600
"""
    return gcode.parse(text)


def test_thick_wide_bead_has_a_more_conductive_neck():
    constants = thermal.constants(PLA)
    necks = []
    for width, height in [(0.8, 0.3), (0.45, 0.05)]:
        tp = stacked_beads(width, height)
        nodes = thermal._deposits(tp, kinematics.timing(tp), 2.0, constants, PLA)
        np.testing.assert_allclose(nodes["lh"], [height, height])
        np.testing.assert_allclose(nodes["neck_mm2"], [width - height] * 2, atol=1e-6)
        necks.append(min(nodes["neck_mm2"]) * 1e-6)

    entry = load_data("thermal")["process"]["bead_contact_conductance"]
    face = np.full(2, 1e-6)
    fixed_distance = np.full(2, 0.2e-3)
    bulk = constants["k"] * constants["z_ratio"] * face / fixed_distance
    for coefficient in (entry["range"][0], constants["contact"], entry["range"][1]):
        joint = thermal._joint_conductance(face, np.array(necks), fixed_distance, {**constants, "contact": coefficient})
        np.testing.assert_allclose(joint, 1 / (1 / bulk + 1 / (coefficient * np.array(necks))))
        assert joint[0] > joint[1]


def test_wider_neck_cools_the_upper_bead_faster_into_the_bed():
    tp = stacked_beads(0.45, 0.2)
    tm = kinematics.timing(tp)
    tp = replace(tp, layer_height=np.full_like(tp.layer_height, np.nan))
    narrow = Profile.from_dict({"nozzle": 0.4, "layer_height": 0.2})
    wide = Profile.from_dict({"nozzle": 0.8, "layer_height": 0.2})
    settings = {"nozzle_c": 220.0, "h_still": 0.0, "h_fan": {0: 0, 100: 0}, "emissivity": 0.0,
                "bed_contact": 5000.0, "nozzle_h": 0.0, "chamber_c": 25.0, "bed_c": 25.0}
    slow = thermal.simulate(tp, tm, narrow, overrides=settings)
    fast = thermal.simulate(tp, tm, wide, overrides=settings)
    np.testing.assert_array_equal(fast.volume, slow.volume)
    np.testing.assert_array_equal(fast.z, slow.z)
    assert fast.joint_c[1, 2] < slow.joint_c[1, 2]


def test_a_plate_cools_through_its_top(monkeypatch):
    """No bed, fan, radiation or nozzle: the middle of a plate (too far from the edges for their heat
    loss to reach it in that time) cools only through its top, exponentially with tau = rho·c·lh / h."""
    tp = gcode.parse(plate_then_dot(20.0))
    tm = kinematics.timing(tp)
    c = {"h_still": 30.0, "h_fan": {0: 0, 100: 0}, "emissivity": 0.0, "bed_contact": 0.0, "nozzle_h": 0.0,
         "cp_melt": 1500.0, "cp_glassy": 1500.0, "latent": 0.0, "chamber_c": 25.0, "bed_c": 25.0}
    monkeypatch.setitem(thermal.TIERS, "exact", {**thermal.TIERS["fine"], "dt_min": 0.05, "dt_max": 0.1})
    h = thermal.simulate(tp, tm, PLA, "exact", overrides=c)
    middle = np.flatnonzero((h.layer == 0) & np.isfinite(h.cover_c))
    assert len(middle) >= 1 and np.all(np.abs(h.xy[middle] - 6) < 1.5)
    tau = 1240 * 1500 * 0.2e-3 / 30
    expected = 25 + (h.birth_c[middle] - 25) * np.exp(-h.cover_s[middle] / tau)
    assert h.cover_c[middle] == pytest.approx(expected, rel=0.03)
    assert np.all(h.cover_s[middle] > 20)


def test_thin_pin_is_covered_hotter_than_the_block(orca, quick):
    tp, _ = orca
    pin, block = quick.z > PIN_Z, (quick.z > 2) & (quick.z < 5.5)
    hot_pin, hot_block = np.nanmedian(quick.cover_c[pin]), np.nanmedian(quick.cover_c[block])
    assert hot_pin > hot_block + 40  # ~1.5 s per pin layer at 100 % fan vs ~5 s per block layer
    assert hot_block > quick.tg_c  # but the block layers are still above Tg when covered
    spots = gcode.spot_heat(tp, lambda x: x, PLA)  # the simpler line-by-line check agrees on which is hotter
    assert np.median(spots["temp_c"][spots["z"] > PIN_Z]) > np.median(spots["temp_c"][(spots["z"] > 2) & (spots["z"] < 5.5)])
    assert np.all(quick.birth_c < 220.5) and np.all(quick.birth_c > 200)  # set 220 C, a little less at high flow
    # Joints: the pin's stay above Tg longer, they cool from a hotter start.
    assert np.nanmedian(quick.above_tg_s[pin]) > np.nanmedian(quick.above_tg_s[block])
    assert np.all(np.diff(np.nanmedian(quick.joint_c[pin], axis=0)) < 0.5)  # T_int falls with time


def test_quick_and_fine_agree(orca, quick):
    fine = thermal.simulate(*orca, PLA, "fine")
    for m_q, m_f in ((quick.z > PIN_Z, fine.z > PIN_Z), ((quick.z > 2) & (quick.z < 5.5), (fine.z > 2) & (fine.z < 5.5))):
        assert np.nanmedian(quick.cover_c[m_q]) == pytest.approx(np.nanmedian(fine.cover_c[m_f]), rel=0.1)
    assert quick.seconds < 10  # the quick tier's time budget on a small part (~60 s in the browser)


def test_old_layers_merge_without_changing_much(orca, quick, monkeypatch):
    monkeypatch.setitem(thermal.TIERS, "short", {**thermal.TIERS["quick"], "layers": 6})
    short = thermal.simulate(*orca, PLA, "short")
    assert np.nanmedian(short.cover_c) == pytest.approx(np.nanmedian(quick.cover_c), rel=0.05)


def test_the_fan_cools(orca, quick):
    still = thermal.simulate(*orca, PLA, "quick", overrides={"h_fan": {0: 0, 100: 0}})
    assert np.nanmedian(still.cover_c) > np.nanmedian(quick.cover_c) + 10
    part_joints = np.isfinite(quick.joint_z)
    np.testing.assert_array_equal(still.joint_z, quick.joint_z)
    fanned = thermal.bond_strength(quick.bond[part_joints])
    warm = thermal.bond_strength(still.bond[part_joints])
    assert np.median(fanned) < np.median(warm)


def test_bambu_side_fan_cools_only_on_equipped_printers():
    text = "; generated by BambuStudio\n" + plate_then_dot(2)
    off = gcode.parse(text)
    on = gcode.parse("M106 P2 S255\nM106 P3 S100\n" + text)
    chamber_only = gcode.parse("M106 P3 S100\n" + text)
    a1 = Profile.from_dict({"printer": "bambu_a1"})

    def cover(tp, profile):
        return thermal.simulate(tp, kinematics.timing(tp), profile).cover_c

    assert np.nanmean(cover(on, PLA)) < np.nanmean(cover(off, PLA))
    np.testing.assert_equal(cover(chamber_only, PLA), cover(off, PLA))
    np.testing.assert_equal(cover(on, a1), cover(off, a1))


def test_islands_and_columns(quick):
    assert quick.island.max() + 1 >= int(quick.layer.max()) + 1  # at least one area per layer
    assert len(np.unique(quick.column)) == 1  # the pin stands on the block: one column
    per_layer = [len(np.unique(quick.island[quick.layer == k])) for k in np.unique(quick.layer)]
    assert max(per_layer) >= 1


def test_heat_model_from_the_api():
    api.load((FIXTURES / "part.stl").read_bytes(), "fixture")
    api.load_gcode((FIXTURES / "bambu.gcode.3mf").read_bytes(), [0, 0, 1])
    r = api.dispatch("heat_model", '{"up": [0, 0, 1]}')
    assert r["tier"] == "quick" and r["spots"] > 100 and r["material"] == "pla"
    pts = np.frombuffer(r["points"], np.float32).reshape(-1, 3)
    assert len(pts) == r["spots"]
    assert pts[:, 2].min() >= 0 and pts[:, 2].max() <= 10.2  # in the oriented part's frame
    top = r["hottest"][0]
    assert top["point"][2] > PIN_Z and top["cover_c"] >= r["cover_c"]["p95"]  # the hottest spots are on the pin
    for name, value in r["cover_c"].items():
        low, high = r["range"]["cover_c"][name]
        assert low <= value <= high and low < high
    low, high = r["range"]["bond_median"]
    assert low <= r["bond_median"] <= high
    json.dumps(r, allow_nan=False, default=bytes.hex)
    assert "range" not in api.heat_model([0, 0, 1], ranges=False)
    with pytest.raises(ValueError, match="another orientation"):
        api.heat_model([1, 0, 0])
