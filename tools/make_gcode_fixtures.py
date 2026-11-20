"""Make the G-code fixtures the reader tests use (T-471): one small part sliced by each slicer.

The part is a 16 mm block with a tab sticking out at the top (overhang and bridge lines) and a
Ø3 mm pin on top (small perimeters, and layers so short the slicer slows them down for cooling),
so every cause of a slowdown shows up. Output, gzipped to keep the repo small:

    core/tests/gcode/orca.gcode.gz          OrcaSlicer, Bambu P1S profile (CONFIG_BLOCK at the top)
    core/tests/gcode/prusa.gcode.gz         PrusaSlicer, its default printer (settings at the end)
    core/tests/gcode/bambu.gcode.3mf        Bambu Studio "sliced plate" (CONFIG_BLOCK at the top,
                                            project_settings.config next to the G-code)
    core/tests/gcode/part.stl               the part, for tests that load it with its G-code

    python tools/make_gcode_fixtures.py     # needs the slicers in their default install folders

Dev-only. The tests read the files as they are, so they don't need the slicers.
"""

from __future__ import annotations

import gzip
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT / "tools"))

from slicer_check import BBL_FILAMENT, BBL_MACHINE, BBL_PROCESS, SLICERS, export, slice_with  # noqa: E402

from fdmcore.mesh import Mesh, to_binary_stl  # noqa: E402

OUT = ROOT / "core" / "tests" / "gcode"


def fixture_part() -> bytes:
    """Block 16 × 12 × 6 mm, a 6 mm tab out at the top (overhang), a Ø3 × 4 mm pin on top."""
    import numpy as np
    from shapely.geometry import Point, Polygon

    from fdmcore.additions import extrude

    from fdmcore.booleans import _to_manifold

    side = Polygon([(0, 0), (16, 0), (16, 4), (22, 4), (22, 6), (0, 6)])
    block = extrude(side, 0, 12)[:, :, [0, 2, 1]][:, ::-1]  # (x, z, y) -> (x, y, z), winding flipped back
    pin = extrude(Point(8, 6).buffer(1.5, 24), 5, 10)  # into the block: one body (a file loads its largest)
    m = (_to_manifold(block) + _to_manifold(pin)).to_mesh()
    return to_binary_stl(Mesh(np.asarray(m.vert_properties)[:, :3][np.asarray(m.tri_verts)].astype(float)))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    part = fixture_part()
    (OUT / "part.stl").write_bytes(part)
    model = export({}, part)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "part.3mf").write_bytes(model)
        for name in ("orca", "prusa"):
            g = slice_with(name, SLICERS[name], tmp / "part.3mf", tmp / name)
            with gzip.open(OUT / f"{name}.gcode.gz", "wb", compresslevel=9) as f:
                f.write(g.read_bytes())
            print(name, g.stat().st_size // 1024, "kB")
        b, p = SLICERS["bambu"], SLICERS["bambu"]["profiles"]
        sliced = tmp / "bambu" / "plate.gcode.3mf"
        sliced.parent.mkdir()
        subprocess.run([str(b["exe"]), "--slice", "0", "--export-3mf", str(sliced),
                        "--load-settings", f"{p / BBL_MACHINE};{p / BBL_PROCESS}", "--load-filaments", str(p / BBL_FILAMENT),
                        str(tmp / "part.3mf")], capture_output=True, timeout=600, cwd=sliced.parent)
        shutil.copy(sliced, OUT / "bambu.gcode.3mf")
        print("bambu", sliced.stat().st_size // 1024, "kB")


if __name__ == "__main__":
    main()
