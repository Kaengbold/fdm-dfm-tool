# Slicing in the browser (for T-124…T-127), checked 2026-10-29

Goal: the detailed level needs real G-code. Local mode already runs the installed OrcaSlicer
(`slicer_cli.py`). This note is about running a slicer inside the web app later (T-127).

## OrcaSlicer compiled to WebAssembly (all AGPL-3.0, compatible with ours)

| Project | How it is used | Output | Builds | Threads | Node |
|---|---|---|---|---|---|
| [Muon3D slicer engine](https://github.com/Muon-3D/muon3d-slicer-engine) | Web Worker protocol (`EngineConnection`, `engine.request('slice', {configs: {machine, process, filaments}, objects: [{mesh: {positions}}]})`; also `profiles.resolve/validate`) | G-code, stats, preview toolpaths, Orca warnings | GitHub Releases (`engine-st`, `engine-mt`) | single-thread works everywhere; multi-thread needs COOP/COEP | yes (`examples/node-cli`) |
| [OrcaWasm](https://github.com/Hiosdra/OrcaWasm) (OrcaSlicer 2.4.2) | `one-slicer-api` C ABI + TS binding (`createEngine`, `session.init`, `oneslicer_project_slice`) | G-code via project export | GitHub Releases, multi-thread only | needs COOP/COEP (SharedArrayBuffer) | not documented |
| [karazas-slicer](https://github.com/karazas-group/karazas-slicer) (2.4.2) | `kz_slice()`: STL + flattened profiles in | grams (G-code optional build flag) | committed `dist/`, ~6 MB (2 MB gzip) | pthreads | yes |
| [FarmSlicer](https://github.com/cinderblock/FarmSlicer) (2.4.2) | full app (Rust server + React), not a library | G-code | none published | st and mt | per its docs |

Also seen: [slicers.wasm](https://github.com/boolean3d/slicers.wasm) (full Bambu Studio / OrcaSlicer /
PrusaSlicer GUIs in a tab), [OrcaWeb](https://hiosdra.github.io/OrcaWeb/) (engine ~29 MB with OCCT).

## CuraEngine

[cura-wasm](https://github.com/Cloud-CNC/cura-wasm) (Cloud-CNC) compiles CuraEngine with Emscripten but
is archived (last release ~2021). CuraEngine itself can be built with Emscripten. Cura's settings differ
from the OrcaSlicer/Bambu settings our 3MF export uses.

## Recommendation

1. Detailed level first against the local OrcaSlicer CLI (same G-code format as the WASM builds).
2. Browser: try the **Muon3D engine's single-thread build** first: documented worker protocol, Node
   support for our tests, no COOP/COEP needed (GitHub Pages can't set those headers; a service worker
   can, e.g. the coi-serviceworker approach, if the multi-thread build is needed later). It takes Orca
   presets and plain vertex arrays, which we already have. Pin a release; check size and start-up time.
3. CuraEngine only as a fallback: archived build and different settings.

Not verified yet: sizes and speed of the Muon3D release files, G-code parity with desktop OrcaSlicer.
