# FDM DFM Tool for Autodesk Fusion

Checks a body for FDM printing with the same engine as the web app: best print orientation,
printability problems and slicer settings in a side panel. Optionally adds 45° teardrops to
sideways holes as real features in the timeline (a base feature cut from the body).

## Install

1. Install the engine's libraries for Fusion's own Python (it has no numpy). Once, from this folder:

   ```sh
   "%LOCALAPPDATA%\Autodesk\webdeploy\production\<version>\Python\python.exe" FDMDFM\install_deps.py
   ```

   This puts numpy, shapely, pyyaml, scipy and a copy of the engine into `FDMDFM/lib/py<version>/`.
   Run it again after a Fusion update that changes the Python version.
2. In Fusion: **Utilities → Add-Ins → Scripts and Add-Ins → +** (add from disk) and pick the `FDMDFM` folder.
3. Run it. The command **Check for 3D printing** appears under **Utilities → Add-Ins**.

## Use

Pick a solid body of the top-level component, a printer, a material and a goal. The report opens
in a panel on the right. "Print with this direction up" is given in the part's own axes.

## Status

`bridge.py` (engine calls, teardrop geometry, report page) is tested outside Fusion
(`core/tests/test_fusion_bridge.py`) and runs under Fusion's Python 3.14. The Fusion UI part
(`FDMDFM.py`) is checked against Fusion's API stubs but not yet run inside Fusion — reports welcome.
