"""Regenerate core/fdmcore/manifest.json — engine files and content hashes for the web worker.

Usage: python tools/make_manifest.py
"""

import hashlib
import json
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
    (PKG / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    print(f"{len(manifest)} files")
