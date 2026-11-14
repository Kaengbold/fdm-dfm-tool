"""Regenerate core/fdmcore/data/orca_profiles.json.gz — the OrcaSlicer profiles local slicing uses.

OrcaSlicer 2.5 packs its printer / process / filament profiles into ``.opc`` files in its own format,
so we can't read them from the installed program any more. This takes them from OrcaSlicer's source
(JSON files, AGPL-3.0 like this project) at a fixed version and keeps, flattened (``inherits`` merged
in), only what our presets can pick: for every printer and nozzle the machine, every process that fits
it, and per material the filament ``slicer_cli.match_profiles`` chooses.

    python tools/orca_profiles.py                    # downloads the profiles folder of the pinned version (git)
    python tools/orca_profiles.py --source DIR       # or use an OrcaSlicer resources/profiles folder you have
"""

from __future__ import annotations

import argparse
import gzip
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from fdmcore.profile import presets  # noqa: E402
from fdmcore.slicer_cli import BUNDLED, FILAMENT_TYPE, _profiles, fits, match_profiles  # noqa: E402

REPO = "https://github.com/OrcaSlicer/OrcaSlicer.git"
TAG = "v2.5.0"


def download(tag: str, into: Path) -> Path:
    """Only resources/profiles of OrcaSlicer at ``tag`` (a sparse, shallow clone: ~100 MB)."""
    git = ["git", "-c", "advice.detachedHead=false"]
    subprocess.run([*git, "clone", "-q", "--depth", "1", "--filter=blob:none", "--sparse", "--branch", tag, REPO, str(into)], check=True)
    subprocess.run([*git, "-C", str(into), "sparse-checkout", "set", "resources/profiles"], check=True)
    return into / "resources" / "profiles"


def snapshot(resources: Path) -> tuple[dict, list[str]]:
    """{name: {"kind", "data"}} for every profile our printers / nozzles / materials can use, and what's missing."""
    profiles = _profiles(resources)
    keep, missing = set(), []
    data = presets()
    for printer in data["printers"].values():
        for nozzle, n in data["nozzles"].items():
            found = None
            for material in FILAMENT_TYPE:
                try:
                    found = match_profiles(profiles, printer["name"], nozzle, material, n["layer_height"])
                except KeyError as e:
                    missing.append(str(e).strip("'\""))
                    continue
                keep.add(found["filament"])
            if found is None:
                continue
            machine = found["machine"]
            keep.add(machine)
            mdata = profiles[machine]["data"]
            # Every process that fits: which one is used depends on the layer height asked for.
            keep.update(name for name, p in profiles.items()
                        if p["kind"] == "process" and p["own"].get("instantiation") == "true" and fits(p["data"], machine, mdata))
    out = {}
    for name in sorted(keep):
        d = {k: v for k, v in profiles[name]["data"].items() if k != "inherits"}
        d["instantiation"] = "true"
        out[name] = {"kind": profiles[name]["kind"], "data": d}
    return out, sorted(set(missing))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", type=Path, help="an OrcaSlicer resources/profiles folder (default: download it)")
    ap.add_argument("--tag", default=TAG, help=f"OrcaSlicer version to download, or that --source is (default {TAG})")
    args = ap.parse_args()
    with tempfile.TemporaryDirectory(prefix="orca-profiles-") as tmp:
        resources = args.source or download(args.tag, Path(tmp) / "orca")
        profiles, missing = snapshot(resources)
    body = json.dumps({"orca_version": args.tag, "source": REPO, "licence": "AGPL-3.0",
                       "profiles": profiles}, sort_keys=True, separators=(",", ":"))
    BUNDLED.write_bytes(gzip.compress(body.encode(), compresslevel=9, mtime=0))
    print(f"{len(profiles)} profiles, {BUNDLED.stat().st_size // 1024} kB -> {BUNDLED.relative_to(ROOT)}")
    print(f"{len(missing)} combinations OrcaSlicer has no profile for:")
    for m in missing:
        print("  " + m)


if __name__ == "__main__":
    main()
