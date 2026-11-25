"""Reproduce T-604's thermal comparison on the Orca pin and two controlled toolpaths."""

import argparse
import gzip
import json
import sys
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

import numpy as np
from scipy import sparse  # Load before measuring the solver or sampling the memory baseline.

from fdmcore import gcode, kinematics, thermal
from fdmcore.profile import Profile

PROFILE = Profile.from_dict({"material": "pla", "printer": "bambu_p1s"})
CASES = ("pin", "bridge", "transition")
MODES = ("quick", "fine", "refined")


def synthetic(case: str) -> str:
    """Deposit 0.45 x 0.2 mm lines. Later layers cover the bridge or the thin cap."""
    out = ["M83", "M104 S220", "M140 S60", "M106 S180"]
    extrusion_per_mm = 0.2 * (0.45 - 0.2 + np.pi * 0.2 / 4) / (np.pi * 1.75**2 / 4)
    for layer in range(12):
        out += [";LAYER_CHANGE", f"G1 Z{(layer + 1) * 0.2:.1f} F600"]
        lower = layer < 7
        if case == "bridge":
            spans = [(0, 5), (15, 20)] if lower else [(0, 20)]
            ys = np.arange(2.225, 6, 0.45)
        else:
            spans = [(0, 20)] if lower else [(8, 12)]
            ys = np.arange(0.225, 8, 0.45) if lower else np.arange(3.125, 4.5, 0.45)
        out.append("; FEATURE: Bridge" if case == "bridge" and layer == 7 else "; FEATURE: Internal solid infill")
        for left, right in spans:
            for row, y in enumerate(ys):
                x0, x1 = (left, right) if row % 2 == 0 else (right, left)
                out += [f"G1 X{x0} Y{y:.3f} F6000", f"G1 X{x1} E{(right-left)*extrusion_per_mm:.8f} F1800"]
        out.append("G4 P500")
    return "\n".join(out)


def prepare(case: str, pin: bytes):
    if case == "pin":
        tp = gcode.parse(gzip.decompress(pin))
        box, z = (122, 124, 128, 128), 6.4
    else:
        tp = gcode.parse(synthetic(case))
        box, z = (7, 2, 13, 6), 1.4 if case == "bridge" else 1.25
    return tp, kinematics.timing(tp), box, z


def measure(case: str, mode: str, inputs: tuple) -> dict:
    tp, tm, box, z = inputs
    started = perf_counter()
    if mode == "refined":
        result = thermal.refine_hot_region(tp, tm, PROFILE, box)
        heat = result.fine
        extra = {"quick_with_history_s": result.quick.seconds, "fine_with_boundary_s": heat.seconds,
                 "boundary_nodes": result.boundary_nodes, "boundary_links": result.boundary_links}
    else:
        # Same padded, quick-grid-aligned box that the prototype uses.
        padded = tuple(np.r_[np.floor((np.array(box[:2]) - 2) / 2),
                             np.ceil((np.array(box[2:]) + 2) / 2)] * 2)
        heat = thermal.simulate(tp, tm, PROFILE, mode, region=padded if mode == "fine" else None)
        extra = {}
    seconds = perf_counter() - started
    roi = ((heat.xy >= box[:2]) & (heat.xy < box[2:])).all(axis=1) & (heat.z > z)
    covered = roi & np.isfinite(heat.cover_c)
    joints = roi & np.isfinite(heat.joint_z)
    window = thermal.window(heat, PROFILE)
    return {"case": case, "mode": mode, "seconds": seconds, "nodes": len(heat.xy), "steps": heat.steps,
            "peak_cover_c": float(heat.cover_c[covered].max()),
            "hot_pct": 100 * float(np.average(window["too_hot"][covered], weights=heat.volume[covered])),
            "weak_pct": 100 * float(np.average(window["too_cold"][joints], weights=heat.volume[joints])),
            **extra}


def peak_rss_mib() -> float:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class MemoryCounters(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("faults", wintypes.DWORD),
                        ("peak_rss", ctypes.c_size_t), ("rss", ctypes.c_size_t),
                        ("other", ctypes.c_size_t * 6)]

        current = ctypes.windll.kernel32.GetCurrentProcess
        current.restype = wintypes.HANDLE
        memory = ctypes.windll.psapi.GetProcessMemoryInfo
        memory.argtypes = [wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD]
        counters = MemoryCounters()
        counters.size = ctypes.sizeof(counters)
        if not memory(current(), ctypes.byref(counters), counters.size):
            raise ctypes.WinError()
        return counters.peak_rss / 2**20
    import resource

    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (2**20 if sys.platform == "darwin" else 1024)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", choices=CASES)
    parser.add_argument("mode", choices=MODES)
    args = parser.parse_args()
    inputs = prepare(args.case, (ROOT / "core/tests/gcode/orca.gcode.gz").read_bytes())
    baseline = peak_rss_mib()
    stats = measure(args.case, args.mode, inputs)
    print(json.dumps({**stats, "baseline_peak_mib": baseline, "peak_rss_mib": peak_rss_mib()}))
