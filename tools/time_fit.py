"""Fit the print-time model (checks.TIME_EFFICIENCY, LAYER_OVERHEAD_S, START_MIN) to OrcaSlicer.

Slices every sample in its best orientation with the installed OrcaSlicer (P1S and K1C system
profiles, PLA, balanced and speed goals), then grid-searches the constants that make our
estimate closest to the mean of the two printers (log error). Dev-only, needs OrcaSlicer.

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
from fdmcore.profile import presets  # noqa: E402
from fdmcore.slicer_cli import find_orca, match_profiles, slice_3mf  # noqa: E402

PRINTERS = ("bambu_p1s", "creality_k1c")
GOALS = ("balanced", "speed")
CACHE = ROOT / "e2e-output" / "time_fit.json"


def orca_times() -> dict:
    if CACHE.exists():
        return json.loads(CACHE.read_text())
    orca = find_orca()
    resources = orca.parent / "resources" / "profiles"
    out = {}
    for f in sorted((ROOT / "web" / "samples").glob("*.stl")):
        api.load(f.read_bytes(), f.stem)
        up = api.orientations()["orientations"][0]["up"]
        for printer, goal in itertools.product(PRINTERS, GOALS):
            prof = {"printer": printer, "goal": goal}
            settings = {x["key"]: x["value"] for x in api.check(up, prof)["settings"]}
            m = match_profiles(resources, presets()["printers"][printer]["name"], "0.4", "pla", float(settings["layer_height"]))
            out[f"{f.stem}|{printer}|{goal}"] = {"up": up, **slice_3mf(api.export(up, prof, settings), m, orca)}
            print(f.stem, printer, goal, out[f"{f.stem}|{printer}|{goal}"]["print_time_min"], flush=True)
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(out))
    return out


def ours(runs: dict) -> dict:
    out = {}
    for key, r in runs.items():
        part, printer, goal = key.split("|")
        api.load((ROOT / "web" / "samples" / f"{part}.stl").read_bytes(), part)
        out[key] = api.check(r["up"], {"printer": printer, "goal": goal})["stats"]["print_time_min"]
    return out


def main():
    runs = orca_times()
    target = {}
    for key, r in runs.items():
        part, printer, goal = key.split("|")
        target.setdefault((part, goal), []).append(r["print_time_min"])
    target = {k: float(np.mean(v)) for k, v in target.items()}
    best = None
    for eff, over, start in itertools.product((0.12, 0.15, 0.18, 0.2, 0.25, 0.3), (3.0, 5.0, 7.0, 9.0), (1.0, 2.0, 3.0, 5.0)):
        checks.TIME_EFFICIENCY, checks.LAYER_OVERHEAD_S, checks.START_MIN = eff, over, start
        est = ours({k: v for k, v in runs.items() if k.split("|")[1] == PRINTERS[0]})
        err = [np.log(est[k] / target[(k.split("|")[0], k.split("|")[2])]) for k in est]
        score = float(np.sqrt(np.mean(np.square(err))))
        if best is None or score < best[0]:
            best = (score, eff, over, start, err)
        print(f"eff {eff} overhead {over} start {start}: rms log error {score:.3f}")
    score, eff, over, start, err = best
    print(f"\nbest: TIME_EFFICIENCY={eff} LAYER_OVERHEAD_S={over} START_MIN={start} "
          f"(typical error ×{np.exp(score):.2f}, worst ×{np.exp(np.max(np.abs(err))):.2f})")


if __name__ == "__main__":
    main()
