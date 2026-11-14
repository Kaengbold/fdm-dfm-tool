"""Fit the print-time model (checks.TIME_EFFICIENCY, LAYER_OVERHEAD_S, START_MIN) to OrcaSlicer.

Slices every sample in its best orientation with the installed OrcaSlicer (P1S and K1C system
profiles, PLA, balanced and speed goals), then grid-searches the constants that make our
estimate closest to the mean of the two printers (log error). Our own layers are sliced once per
part and only the time formula is re-run per try, so the search takes seconds. Dev-only, needs
OrcaSlicer.

    python tools/time_fit.py              # slice (slow, cached in e2e-output/time_fit.json) and fit
"""

from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from fdmcore import api, checks  # noqa: E402
from fdmcore.checks import orient, slice_mesh  # noqa: E402
from fdmcore.placement import print_rotation  # noqa: E402
from fdmcore.profile import Profile, presets  # noqa: E402
from fdmcore.slicer_cli import find_orca, pick_profiles, slice_3mf  # noqa: E402

PRINTERS = ("bambu_p1s", "creality_k1c")
GOALS = ("balanced", "speed")
CACHE = ROOT / "e2e-output" / "time_fit.json"


def orca_times() -> dict:
    if CACHE.exists():
        return json.loads(CACHE.read_text())
    orca = find_orca()
    out = {}
    for f in sorted((ROOT / "web" / "samples").glob("*.stl")):
        api.load(f.read_bytes(), f.stem)
        up = api.orientations()["orientations"][0]["up"]
        for printer, goal in itertools.product(PRINTERS, GOALS):
            prof = {"printer": printer, "goal": goal}
            settings = {x["key"]: x["value"] for x in api.check(up, prof)["settings"]}
            m = pick_profiles(orca, presets()["printers"][printer]["name"], "0.4", "pla", float(settings["layer_height"]))
            out[f"{f.stem}|{printer}|{goal}"] = {"up": up, **slice_3mf(api.export(up, prof, settings), m, orca)}
            print(f.stem, printer, goal, out[f"{f.stem}|{printer}|{goal}"]["print_time_min"], flush=True)
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(out))
    return out


def our_layers(runs: dict) -> dict:
    """Our sliced layers per run (part, printer, goal): sliced once, the time formula re-runs on them."""
    out = {}
    for key, r in runs.items():
        part, printer, goal = key.split("|")
        api.load((ROOT / "web" / "samples" / f"{part}.stl").read_bytes(), part)
        p = Profile.from_dict({"printer": printer, "goal": goal})
        mesh = api._require().mesh
        rotation, _ = print_rotation(mesh, r["up"], p.printer["bed"])
        out[key] = (slice_mesh(orient(mesh, rotation), p.layer_height, 160), p)
    return out


def main():
    runs = orca_times()
    target = {}
    for key, r in runs.items():
        part, printer, goal = key.split("|")
        target.setdefault((part, goal), []).append(r["print_time_min"])
    spread = [max(v) / min(v) for v in target.values()]
    print(f"P1S vs K1C (OrcaSlicer): typical ×{np.exp(np.mean(np.log(spread))):.2f}, most ×{max(spread):.2f}")
    target = {k: float(np.mean(v)) for k, v in target.items()}
    layers = our_layers({k: v for k, v in runs.items() if k.split("|")[1] == PRINTERS[0]})
    best = None
    for eff, over, start in itertools.product((0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 1.0), (0.0, 1.0, 2.0, 3.0, 5.0, 7.0, 10.0), (0.5, 1.0, 2.0, 3.0, 5.0)):
        checks.TIME_EFFICIENCY, checks.LAYER_OVERHEAD_S, checks.START_MIN = eff, over, start
        err = [np.log(checks._estimate(ls, p)["print_time_min"] / target[(k.split("|")[0], k.split("|")[2])]) for k, (ls, p) in layers.items()]
        score = float(np.sqrt(np.mean(np.square(err))))
        if best is None or score < best[0]:
            best = (score, eff, over, start, err)
    score, eff, over, start, err = best
    print(f"best: TIME_EFFICIENCY={eff} LAYER_OVERHEAD_S={over} START_MIN={start} "
          f"(typical error ×{np.exp(score):.2f}, worst ×{np.exp(np.max(np.abs(err))):.2f}, mean ×{np.exp(np.mean(err)):.2f})")


if __name__ == "__main__":
    main()
