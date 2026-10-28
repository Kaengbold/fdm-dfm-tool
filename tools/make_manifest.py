"""Regenerate core/fdmcore/manifest.json — the list of engine files the web worker loads.

Usage: python tools/make_manifest.py
"""

import json
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "core" / "fdmcore"
LOCAL_ONLY = {"mcp_server.py", "booleans.py"}  # need packages Pyodide doesn't have

if __name__ == "__main__":
    files = sorted(
        rel for p in PKG.rglob("*") if p.suffix in (".py", ".yaml") and (rel := str(p.relative_to(PKG)).replace("\\", "/")) not in LOCAL_ONLY
    )
    (PKG / "manifest.json").write_text(json.dumps(files, indent=1) + "\n")
    print(f"{len(files)} files")
