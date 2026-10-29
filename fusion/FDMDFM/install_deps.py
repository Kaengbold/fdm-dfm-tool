"""Put the engine and its dependencies into lib/py<version>/ next to the add-in, for Fusion's own Python
(which has no numpy). Run once, with Fusion's python.exe or any Python that has pip:

    python install_deps.py [path\\to\\Fusion\\python.exe]

Wheels are picked for Fusion's Python version and platform, so the Python running this script
doesn't have to match. The engine (core/fdmcore) is copied too, so the add-in folder works on
its own after it is moved to Fusion's AddIns folder.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.normpath(os.path.join(HERE, "..", "..", "core", "fdmcore"))
PACKAGES = ["numpy", "shapely", "pyyaml", "scipy"]


def target_python(fusion_python: str | None) -> tuple[str, str]:
    """(version like '3.14', platform tag) of the Python the add-in will run under."""
    exe = fusion_python or sys.executable
    out = subprocess.run([exe, "-c", "import sys,sysconfig;print(f'{sys.version_info[0]}.{sys.version_info[1]}',sysconfig.get_platform())"],
                         capture_output=True, text=True, check=True).stdout.split()
    return out[0], out[1].replace("-", "_").replace(".", "_")


def main(argv: list[str]) -> None:
    version, platform = target_python(argv[1] if len(argv) > 1 else None)
    lib = os.path.join(HERE, "lib", f"py{version}")  # same name as bridge.LIB
    if subprocess.run([sys.executable, "-m", "pip", "--version"], capture_output=True).returncode:
        subprocess.run([sys.executable, "-m", "ensurepip", "--user"], check=True)
    subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "--target", lib,
                    "--python-version", version, "--platform", platform, "--only-binary=:all:", *PACKAGES], check=True)
    if os.path.isdir(ENGINE):
        shutil.copytree(ENGINE, os.path.join(lib, "fdmcore"), dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    print(f"Installed {', '.join(PACKAGES)} for Python {version} ({platform}) into {lib}")


if __name__ == "__main__":
    main(sys.argv)
