"""Regenerate core/fdmcore/data/orca_printers.json — every printer OrcaSlicer has a profile for, for "My printer
isn't listed" in the app (T-451): its bed and height, nozzles, whether the bed moves (from the frame type) and how
fast it prints (from the outer wall speed of its own OrcaSlicer process profiles). What a profile doesn't say (enclosure, nozzle material, multi-material)
the app asks or marks as assumed. From OrcaSlicer's source at the same pinned version as tools/orca_profiles.py.

    python tools/orca_printers.py                    # downloads the profiles folder of the pinned version (git)
    python tools/orca_printers.py --source DIR       # or use an OrcaSlicer resources/profiles folder you have
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT / "tools"))

from fdmcore.profile import presets  # noqa: E402
from fdmcore.slicer_cli import _profiles, fits  # noqa: E402
from orca_profiles import REPO, TAG, download  # noqa: E402

OUT = ROOT / "core" / "fdmcore" / "data" / "orca_printers.json"
# Frame types: a bed slinger (i3) moves the part back and forth; CoreXY / H-bot / delta keep the bed still in X and Y.
MOVING_BED = {"i3": True, "corexy": False, "hbot": False, "delta": False}
# The speed classes in presets.yaml print outer walls at 200 (fast) and 140 mm/s (medium): a printer whose own
# 0.2 mm process profiles print them at 170 mm/s or more is "fast". Firmware acceleration limits don't tell: the
# MK3S profile says 10000 mm/s², the printer runs about 1000.
FAST_WALL_MM_S = 170


def _first(v):
    return v[0] if isinstance(v, list) and v else v


def _num(v) -> float | None:
    try:
        return float(_first(v))
    except (TypeError, ValueError):
        return None


def catalogue(resources: Path) -> list[dict]:
    """One entry per printer model, from its 0.4 mm nozzle profile (or the first one there is)."""
    profiles = _profiles(resources)
    processes = [p["data"] for p in profiles.values() if p["kind"] == "process" and p["own"].get("instantiation") == "true"]
    machines: dict[str, list[dict]] = {}
    for name, p in profiles.items():
        d = p["data"]
        if p["kind"] != "machine" or p["own"].get("instantiation") != "true" or not d.get("printable_area"):
            continue
        vendor = p["path"].relative_to(resources).parts[0]  # the profile folder: BBL, Creality, …
        machines.setdefault(d.get("printer_model") or name, []).append({**d, "_vendor": vendor, "_name": name})
    built_in = {p["name"].lower() for p in presets()["printers"].values()}
    out = []
    for model, variants in sorted(machines.items(), key=lambda kv: kv[0].lower()):
        if model.lower() in built_in:
            continue
        d = next((v for v in variants if _first(v.get("nozzle_diameter")) == "0.4"), variants[0])
        try:
            pts = [tuple(map(float, xy.split("x"))) for xy in d["printable_area"]]
        except ValueError:  # a broken bed outline in the profile: skip the printer
            continue
        xs, ys = [x for x, _ in pts], [y for _, y in pts]
        height = _num(d.get("printable_height"))
        if not height:
            continue
        walls = sorted(w for q in processes if abs((_num(q.get("layer_height")) or 0) - 0.2) < 0.01
                       and (w := _num(q.get("outer_wall_speed"))) and fits(q, d["_name"], d))
        drive = _first(d.get("extruder_type"))
        nozzles = sorted({_first(v.get("nozzle_diameter")) for v in variants if _first(v.get("nozzle_diameter"))}, key=float)
        out.append({
            "name": model,
            "vendor": d["_vendor"],
            "bed": [round(max(xs) - min(xs)), round(max(ys) - min(ys)), round(height)],
            "nozzles": nozzles,
            "moving_bed": MOVING_BED.get(str(d.get("printer_structure"))),
            "direct_drive": {"Direct Drive": True, "Bowden": False}.get(drive),
            "speed": None if not walls else "fast" if walls[len(walls) // 2] >= FAST_WALL_MM_S else "medium",
        })
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", type=Path, help="an OrcaSlicer resources/profiles folder (default: download it)")
    ap.add_argument("--tag", default=TAG, help=f"OrcaSlicer version to download, or that --source is (default {TAG})")
    args = ap.parse_args()
    with tempfile.TemporaryDirectory(prefix="orca-printers-") as tmp:
        printers = catalogue(args.source or download(args.tag, Path(tmp) / "orca"))
    body = {"orca_version": args.tag, "source": REPO, "licence": "AGPL-3.0", "printers": printers}
    OUT.write_text(json.dumps(body, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"{len(printers)} printers -> {OUT.relative_to(ROOT)} ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
