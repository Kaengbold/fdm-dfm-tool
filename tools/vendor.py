"""Download the pinned browser libraries so the site can serve them itself (T-072): no CDN at run time.

Writes a folder that mirrors the CDN paths (``vendor/npm/three@0.170.0/...``, ``vendor/pyodide/v0.27.2/full/...``),
so the app only swaps its base URL ``https://cdn.jsdelivr.net`` for ``../vendor`` (see .github/workflows/pages.yml).
Pyodide gets its runtime plus the packages the engine loads and their dependencies, read from pyodide-lock.json.

Every file is checked before it is used (T-312): the files below against the sha256 pinned here, the
Pyodide packages against the sha256 in the (pinned) pyodide-lock.json. A changed file on the CDN, or a
tampered cache, stops the build instead of reaching the site. After changing a version, run
``python tools/vendor.py --hashes <dir>`` and paste the new lines into HASHES.

Usage: python tools/vendor.py <out-dir>   (or --hashes <dir>)
"""

import hashlib
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
HASHES = {
    "npm/three@0.170.0/build/three.module.js": "ce1fa418de16a19495a9f72495580e3015d7745c296d3ce0485897f902ddedfb",
    "npm/three@0.170.0/examples/jsm/controls/OrbitControls.js": "80efaadea4f8a636a65fb0bd08bfef62f3d93a0bb94e2e7500f23176c5c07f4e",
    "npm/manifold-3d@3.5.4/manifold.js": "ca3c8a88df63bb3083013d1eb7f700bf23216594f6e98a2566e34f55d7341ce8",
    "npm/manifold-3d@3.5.4/manifold.wasm": "73e3b419ad31294f6b1cc478173944e50efc4a34bfdeeff0107bdfc12975f11c",
    "npm/occt-import-js@0.0.23/dist/occt-import-js.js": "3fb44ce11d00611f9b3f3c5775d520ebab48930c1f08279b7b1316f05f0d3379",
    "npm/occt-import-js@0.0.23/dist/occt-import-js.wasm": "33391fc9d94ea5c869a6718488bf0a9a464222bac9bdc764dfe1690cef281952",
    "npm/replicad-opencascadejs@1.1.0/dist/replicad_single.js": "018e57538fcb773f0124eae60aee4de693d3aae01c55b2b883a874911ab121c3",
    "npm/replicad-opencascadejs@1.1.0/dist/replicad_single.wasm": "4c9f22e9f3828dca6f3c95405934cdbe624e593c35266f47f392ab337478dbde",
    "pyodide/v0.27.2/full/pyodide.mjs": "0a7aa1048a1349f9f9a8966f15ce224048de93b5292b9d66975b807c54d416b4",
    "pyodide/v0.27.2/full/pyodide.asm.js": "c5bc43242dbdd922b03fd9f9ca22480d37451238686e84cb38499143f81ec75b",
    "pyodide/v0.27.2/full/pyodide.asm.wasm": "7936ef6019d80723adce4bd86666b84534d071362839f1cf9b2b80bed917c812",
    "pyodide/v0.27.2/full/python_stdlib.zip": "7a4edf94cc3d937ba29078adb857d1646d9ce064ad93e795645d124e212eb5a1",
    "pyodide/v0.27.2/full/pyodide-lock.json": "3a3cfb1c0bc02917a1792c1ecdf2b9b484ca406f5d03cb8af4083e97ec7d706a",
}


def fetch(path: str, out: Path, sha256: str | None = None) -> bytes:
    """The file at ``path``, downloaded once, checked against ``sha256`` every time (also from the cache)."""
    target = out / path
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(f"{CDN}/{path}", timeout=120) as res:
            target.write_bytes(res.read())
    data = target.read_bytes()
    if sha256 is not None and hashlib.sha256(data).hexdigest() != sha256:
        target.unlink()
        raise SystemExit(f"{path}: sha256 does not match the pinned one; not using it")
    return data


def pyodide_packages(lock: dict) -> dict[str, str]:
    """The packages to ship (file name -> sha256): the ones the engine loads and everything they depend on."""
    pkgs = lock["packages"]
    todo, done = list(PACKAGES), set()
    while todo:
        name = todo.pop()
        if name in done:
            continue
        done.add(name)
        todo += pkgs[name]["depends"]
    return {pkgs[n]["file_name"]: pkgs[n]["sha256"] for n in done}


def main(out: Path) -> None:
    for path in FILES + [PYODIDE + name for name in PYODIDE_RUNTIME]:
        fetch(path, out, HASHES[path])
    lock = json.loads((out / PYODIDE / "pyodide-lock.json").read_text())
    for name, sha256 in sorted(pyodide_packages(lock).items()):
        fetch(PYODIDE + name, out, sha256)
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"{out}: {size / 1e6:.0f} MB")


def print_hashes(out: Path) -> None:
    """The HASHES lines for the current FILES / PYODIDE_RUNTIME (after a version change)."""
    for path in FILES + [PYODIDE + name for name in PYODIDE_RUNTIME]:
        print(f'    "{path}": "{hashlib.sha256(fetch(path, out)).hexdigest()}",')


if __name__ == "__main__":
    if sys.argv[1] == "--hashes":
        print_hashes(Path(sys.argv[2]))
    else:
        main(Path(sys.argv[1]))
