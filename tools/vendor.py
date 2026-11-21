"""Download the pinned browser libraries so the site can serve them itself (T-072): no CDN at run time.

Writes a folder that mirrors the CDN paths (``vendor/npm/three@0.170.0/...``, ``vendor/pyodide/v0.27.2/full/...``)
plus the single-thread Muon3D Slicer Engine (``vendor/muon3d-slicer-engine@0.6.0-m1.1/...``, T-525),
so the app only swaps its base URL ``https://cdn.jsdelivr.net`` for ``../vendor`` (see .github/workflows/pages.yml).
Pyodide gets its runtime plus the packages the engine loads and their dependencies, read from pyodide-lock.json.

Every file is checked before it is used (T-312): the files below against the sha256 pinned here, the
Pyodide packages against the sha256 in the (pinned) pyodide-lock.json, and the Muon3D release tarball
plus every extracted file against their pinned sha256. A changed file, or a tampered cache, stops the build
instead of reaching the site. After changing a version, run ``python tools/vendor.py --hashes <dir>`` and
paste the new lines into HASHES.

Usage: python tools/vendor.py <out-dir>
       python tools/vendor.py --slicer-only <out-dir> [--tarball <tgz>]
       python tools/vendor.py --hashes <dir>
"""

import argparse
import hashlib
import io
import json
import ssl
import sys
import tarfile
import urllib.error
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

# Muon3D Slicer Engine (AGPL-3.0-only, OrcaSlicer 2.5.0-dev @ 2d1163eb6f): single-thread build only
# (works in every browser without COOP/COEP headers). Not on a CDN; fetched from GitHub Releases.
MUON3D_VERSION = "0.6.0-m1.1"
MUON3D_DIR = f"muon3d-slicer-engine@{MUON3D_VERSION}"
MUON3D_PREFIX = f"muon3d-slicer-engine-{MUON3D_VERSION}/"
MUON3D_URL = (
    f"https://github.com/Muon-3D/muon3d-slicer-engine/releases/download/"
    f"v{MUON3D_VERSION}/muon3d-slicer-engine-{MUON3D_VERSION}.tgz"
)
MUON3D_TARBALL_SHA256 = "6f666027b59c4d53c067890839abefbcbc06789a5536d4fcde6003a215e69c08"
MUON3D_FILES = [
    "manifest.json",
    "host.446c6d131da98dbf.js",
    "host-chunk.bd6a59f31a9394b2.js",
    "engine-st.mjs",
    "engine-st.wasm",
    "LICENSE",
    "NOTICE",
    "SOURCE.md",
    "THIRD-PARTY-NOTICES.md",
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
    f"{MUON3D_DIR}/manifest.json": "8e6bb1925975d89e994a57f667efef5607a094c7e3befdcdf6bfb6a697ce8efb",
    f"{MUON3D_DIR}/host.446c6d131da98dbf.js": "446c6d131da98dbf2bbb5e102472ad6e3e192b0d7e8af19103070528d4a1a6f2",
    f"{MUON3D_DIR}/host-chunk.bd6a59f31a9394b2.js": "bd6a59f31a9394b26f524f5dfcf1e2757db814e437cb019f925acd89b0720b74",
    f"{MUON3D_DIR}/engine-st.mjs": "46602f1e095e45ddd8a96667486d0f4210dd967752e383e36207187474c5f53d",
    f"{MUON3D_DIR}/engine-st.wasm": "290e5825b14a0e9cbed453304af32644c2f1e269e413cf0bf37d7efc5c1ab6ea",
    f"{MUON3D_DIR}/LICENSE": "57c8ff33c9c0cfc3ef00e650a1cc910d7ee479a8bc509f6c9209a7c2a11399d6",
    f"{MUON3D_DIR}/NOTICE": "47d74fd64af4e689f984d86e440b1dc7a63198a6a5a6cbc6e98be40b1b5951bc",
    f"{MUON3D_DIR}/SOURCE.md": "c5d0dd3dd65d201be06d690d4631f4141f584f95dcf0bcc2bf3e0715ae259c89",
    f"{MUON3D_DIR}/THIRD-PARTY-NOTICES.md": "de55c04a6156ae933a2219e3accc5a11149cfd4206d4fab74b03df48920f8fc6",
}


def _download(url: str, timeout: int = 120) -> bytes:
    """Download ``url`` with TLS verification; fall back to certifi if system roots lack an intermediate."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as res:
            return res.read()
    except urllib.error.URLError as err:
        if not isinstance(err.reason, ssl.SSLCertVerificationError):
            raise
        try:
            import certifi
        except ImportError:
            raise err
        ctx = ssl.create_default_context(cafile=certifi.where())
        with urllib.request.urlopen(url, timeout=timeout, context=ctx) as res:
            return res.read()


def fetch(path: str, out: Path, sha256: str | None = None) -> bytes:
    """The file at ``path``, downloaded once, checked against ``sha256`` every time (also from the cache)."""
    target = out / path
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_download(f"{CDN}/{path}", timeout=120))
    data = target.read_bytes()
    if sha256 is not None and hashlib.sha256(data).hexdigest() != sha256:
        target.unlink()
        raise SystemExit(f"{path}: sha256 does not match the pinned one; not using it")
    return data


def fetch_muon3d(out: Path, tarball: Path | None = None, verify: bool = True) -> None:
    """Extract and verify the single-thread Muon3D Slicer Engine runtime into ``out / MUON3D_DIR``."""
    dest = out / MUON3D_DIR
    cached_ok = all((dest / name).is_file() for name in MUON3D_FILES)
    if cached_ok:
        for name in MUON3D_FILES:
            rel = f"{MUON3D_DIR}/{name}"
            data = (dest / name).read_bytes()
            if verify and hashlib.sha256(data).hexdigest() != HASHES[rel]:
                (dest / name).unlink()
                raise SystemExit(f"{rel}: sha256 does not match the pinned one; not using it")
        return

    raw = tarball.read_bytes() if tarball is not None else _download(MUON3D_URL, timeout=300)
    if verify and hashlib.sha256(raw).hexdigest() != MUON3D_TARBALL_SHA256:
        raise SystemExit(f"{MUON3D_DIR} tarball: sha256 does not match the pinned one; not using it")

    dest.mkdir(parents=True, exist_ok=True)
    wanted = {MUON3D_PREFIX + name: name for name in MUON3D_FILES}
    extracted: set[str] = set()
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
        for member in tf.getmembers():
            name = wanted.get(member.name)
            if name is None or not member.isfile():
                continue
            handle = tf.extractfile(member)
            if handle is None:
                continue
            data = handle.read()
            rel = f"{MUON3D_DIR}/{name}"
            if verify and hashlib.sha256(data).hexdigest() != HASHES[rel]:
                raise SystemExit(f"{rel}: sha256 does not match the pinned one; not using it")
            (dest / name).write_bytes(data)
            extracted.add(name)

    missing = sorted(set(MUON3D_FILES) - extracted)
    if missing:
        raise SystemExit(f"{MUON3D_DIR} tarball is missing expected files: {', '.join(missing)}")


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


def main(out: Path, slicer_only: bool = False, tarball: Path | None = None) -> None:
    if not slicer_only:
        for path in FILES + [PYODIDE + name for name in PYODIDE_RUNTIME]:
            fetch(path, out, HASHES[path])
        lock = json.loads((out / PYODIDE / "pyodide-lock.json").read_text())
        for name, sha256 in sorted(pyodide_packages(lock).items()):
            fetch(PYODIDE + name, out, sha256)
    fetch_muon3d(out, tarball=tarball)
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"{out}: {size / 1e6:.1f} MB")


def print_hashes(out: Path, tarball: Path | None = None) -> None:
    """The HASHES lines for the current FILES / PYODIDE_RUNTIME / MUON3D_FILES (after a version change)."""
    for path in FILES + [PYODIDE + name for name in PYODIDE_RUNTIME]:
        print(f'    "{path}": "{hashlib.sha256(fetch(path, out)).hexdigest()}",')
    fetch_muon3d(out, tarball=tarball, verify=False)
    for name in MUON3D_FILES:
        rel = f"{MUON3D_DIR}/{name}"
        digest = hashlib.sha256((out / rel).read_bytes()).hexdigest()
        print(f'    f"{{MUON3D_DIR}}/{name}": "{digest}",')


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out", type=Path, help="output vendor directory")
    ap.add_argument("--hashes", action="store_true", help="print HASHES lines for the current pinned files")
    ap.add_argument("--slicer-only", action="store_true", help="only vendor the Muon3D browser slicer engine")
    ap.add_argument("--tarball", type=Path, help="local Muon3D release tarball instead of downloading it")
    args = ap.parse_args()
    if args.hashes:
        print_hashes(args.out, tarball=args.tarball)
    else:
        main(args.out, slicer_only=args.slicer_only, tarball=args.tarball)

