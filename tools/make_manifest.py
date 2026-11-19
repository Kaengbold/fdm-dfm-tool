"""Regenerate core/fdmcore/manifest.json — the engine files the web worker loads.

Usage: python tools/make_manifest.py            # file list (committed; changes only when files are added / removed)
       python tools/make_manifest.py --hashes   # {file: content hash} (Pages build only)

With hashes the worker and sw.js cache each file until its content changes. They are not
committed: every engine edit would then need a re-run, parallel branches would conflict on
this file, and a stale hash would serve old code to a local dev server.
"""

import hashlib
import json
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "core" / "fdmcore"
LOCAL_ONLY = {"mcp_server.py", "booleans.py", "slicer_cli.py", "cli.py"}  # need packages Pyodide doesn't have, or run programs


def file_hash(path: Path) -> str:
    """Short content hash with LF normalisation so Windows and Linux checkouts agree."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()[:12]


def build_manifest(pkg: Path = PKG) -> dict[str, str]:
    entries = sorted(
        (rel, file_hash(p))
        for p in pkg.rglob("*")
        if p.suffix in (".py", ".yaml") and (rel := str(p.relative_to(pkg)).replace("\\", "/")) not in LOCAL_ONLY
    )
    return dict(entries)


if __name__ == "__main__":
    manifest = build_manifest()
    if "--hashes" not in sys.argv:
        manifest = list(manifest)
    (PKG / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    print(f"{len(manifest)} files")
