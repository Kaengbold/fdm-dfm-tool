"""Download the pinned browser libraries so the site can serve them itself (T-072): no CDN at run time.

Writes a folder that mirrors the CDN paths (``vendor/npm/three@0.170.0/...``, ``vendor/pyodide/v0.27.2/full/...``),
so the app only swaps its base URL ``https://cdn.jsdelivr.net`` for ``../vendor`` (see .github/workflows/pages.yml).
Pyodide gets its runtime plus the packages the engine loads and their dependencies, read from pyodide-lock.json.

Usage: python tools/vendor.py <out-dir>
"""

import json
import sys
import urllib.request
from pathlib import Path

CDN = "https://cdn.jsdelivr.net"
PYODIDE = "pyodide/v0.27.2/full/"
PYODIDE_RUNTIME = ["pyodide.mjs", "pyodide.asm.js", "pyodide.asm.wasm", "python_stdlib.zip", "pyodide-lock.json"]
PACKAGES = ["numpy", "shapely", "pyyaml", "scipy"]  # web/worker.js, plus scipy loaded on demand (NEEDS_SCIPY)
FILES = [
    "npm/three@0.170.0/build/three.module.js",
    "npm/three@0.170.0/examples/jsm/controls/OrbitControls.js",
    "npm/manifold-3d@3.5.4/manifold.js",
    "npm/manifold-3d@3.5.4/manifold.wasm",
    "npm/occt-import-js@0.0.23/dist/occt-import-js.js",
    "npm/occt-import-js@0.0.23/dist/occt-import-js.wasm",
    "npm/replicad-opencascadejs@1.1.0/dist/replicad_single.js",
    "npm/replicad-opencascadejs@1.1.0/dist/replicad_single.wasm",
]


def fetch(path: str, out: Path) -> bytes:
    target = out / path
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(f"{CDN}/{path}", timeout=120) as res:
            target.write_bytes(res.read())
    return target.read_bytes()


def pyodide_packages(lock: dict) -> set[str]:
    """The packages to ship: the ones the engine loads and everything they depend on."""
    pkgs = lock["packages"]
    todo, done = list(PACKAGES), set()
    while todo:
        name = todo.pop()
        if name in done:
            continue
        done.add(name)
        todo += pkgs[name]["depends"]
    return {pkgs[n]["file_name"] for n in done}


def main(out: Path) -> None:
    for path in FILES:
        fetch(path, out)
    for name in PYODIDE_RUNTIME:
        fetch(PYODIDE + name, out)
    lock = json.loads((out / PYODIDE / "pyodide-lock.json").read_text())
    for name in sorted(pyodide_packages(lock)):
        fetch(PYODIDE + name, out)
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"{out}: {size / 1e6:.0f} MB")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
