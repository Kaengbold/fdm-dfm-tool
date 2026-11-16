"""List shape-sensitive settings for every shipped STL, in its recommended orientation.

Run before and after a change with --output / --baseline to compare the same parts.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from fdmcore import api

KEYS = ("wall_loops", "sparse_infill_density", "top_shell_layers")


def audit() -> dict:
    out = {}
    paths = sorted((ROOT / "web/samples").glob("*.stl")) + sorted((ROOT / "core/tests/corpus").glob("*.stl"))
    profile = {"printer": "bambu_p1s", "material": "pla", "goal": "balanced"}
    for path in paths:
        api.load(path.read_bytes(), path.stem)
        up = api.orientations(profile)["orientations"][0]["up"]
        check = api.check(up, profile)
        settings = {s["key"]: s for s in check["settings"]}
        out[path.stem] = {"triple": [settings[k]["value"] for k in KEYS], "up": up,
                          "reasons": {k: settings[k]["reason"] for k in KEYS}, "stats": check["stats"]}
        print(f"{path.stem}: {tuple(out[path.stem]['triple'])}", flush=True)
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    results = audit()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"{len(results)} parts; {len({tuple(r['triple']) for r in results.values()})} different triples")
    if args.baseline:
        before = json.loads(args.baseline.read_text(encoding="utf-8"))
        print("\n| Part | Before (walls, infill, top) | After |\n|---|---|---|")
        for name, row in results.items():
            print(f"| {name} | {', '.join(before[name]['triple'])} | {', '.join(row['triple'])} |")
