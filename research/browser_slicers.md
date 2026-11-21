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

## Decision and measurements (2026-11-21, T-525)

Checked the latest releases of both candidates on 2026-11-21:

| Candidate | Latest release | Upstream OrcaSlicer | Licence | Single-thread build | Size (raw / gzip / brotli) | COOP/COEP needed |
|---|---|---|---|---|---|---|
| **Muon3D Slicer Engine** | `v0.6.0-m1.1` (2026-11-18) | `2.5.0-dev` (`2d1163eb6f`) | **AGPL-3.0-only** (compatible with our AGPL-3.0-or-later) | Yes (`engine-st.wasm` + `engine-st.mjs` + worker host) | 11.6 MB vendored (`engine-st.wasm` 11.07 MB / 3.42 MB gz / 2.34 MB br) | **No** |
| **OrcaWasm** | `wasm-v2.4.2-patch29-multithreaded` (2026-10-08) | `2.4.2` | AGPL-3.0 | No (multi-thread `slicer-mt.wasm` only) | 29.0 MB (`slicer-mt.wasm`) | **Yes** (`SharedArrayBuffer`) |

**Chosen: Muon3D Slicer Engine `v0.6.0-m1.1` (`engine-st`).**
- Single-thread build works on plain GitHub Pages without COOP/COEP headers or a `coi-serviceworker` reload shim, and is CSP-safe (`'wasm-unsafe-eval'`, no `eval`).
- Much smaller (~2.5× smaller raw than OrcaWasm; ~3.5 MB over the wire with gzip).
- Built from OrcaSlicer `2.5.0-dev`, matching our bundled `core/fdmcore/data/orca_profiles.json.gz` (extracted from OrcaSlicer 2.5.0).
- Clean Web Worker v2.4 protocol (`hello`, `load`, `config.definitions`, `slice`) with progress events, structured error codes (including OOM `code: 2`), and Node support for offline tests.
- Reproducible CI release with upstream `LICENSE`, `NOTICE`, `SOURCE.md` and `THIRD-PARTY-NOTICES.md`.

### Pinned vendoring (`tools/vendor.py`)

Fetched from `https://github.com/Muon-3D/muon3d-slicer-engine/releases/download/v0.6.0-m1.1/muon3d-slicer-engine-0.6.0-m1.1.tgz`
(tarball sha256 `6f666027b59c4d53c067890839abefbcbc06789a5536d4fcde6003a215e69c08`), keeping only the
9 files required for single-thread browser slicing + AGPL notices (`11.6 MB` uncompressed in
`vendor/muon3d-slicer-engine@0.6.0-m1.1/`):

- `manifest.json` (8,270 B) — `sha256: 8e6bb1925975d89e994a57f667efef5607a094c7e3befdcdf6bfb6a697ce8efb`
- `host.446c6d131da98dbf.js` (44,263 B) — `sha256: 446c6d131da98dbf2bbb5e102472ad6e3e192b0d7e8af19103070528d4a1a6f2`
- `host-chunk.bd6a59f31a9394b2.js` (905 B) — `sha256: bd6a59f31a9394b26f524f5dfcf1e2757db814e437cb019f925acd89b0720b74`
- `engine-st.mjs` (95,015 B) — `sha256: 46602f1e095e45ddd8a96667486d0f4210dd967752e383e36207187474c5f53d`
- `engine-st.wasm` (11,070,968 B; 3.42 MB gz / 2.34 MB br) — `sha256: 290e5825b14a0e9cbed453304af32644c2f1e269e413cf0bf37d7efc5c1ab6ea`
- `LICENSE`, `NOTICE`, `SOURCE.md`, `THIRD-PARTY-NOTICES.md` (383,852 B total)

`web/slicer-worker.js` fetches `manifest.json` with SRI (`integrity`), verifies the pinned `host`,
`engine-st.mjs` and `engine-st.wasm` sha256s in the manifest, and `web/sw.js` caches the vendored
files and evicts older `/vendor/muon3d-slicer-engine@<ver>/` entries on upgrade.

### Measured start-up, slicing time, and desktop OrcaSlicer parity

Measured via `python tools/test_browser_slicer.py --all-samples` in headless Edge (local server
only, all external URLs blocked) and compared against local desktop OrcaSlicer
(`C:\Program Files\OrcaSlicer\orca-slicer.exe`) with the same bundled profiles
(`Bambu Lab P1S 0.4 nozzle` / `0.20mm Standard @BBL X1C` / `Generic PLA`, overrides
`wall_loops=3`, `sparse_infill_density=15%`, `sparse_infill_pattern=gyroid`):

- **Browser warm-up (lazy worker spawn + WASM init + profiles gunzip):** `init = 364 ms`, total wall `477 ms` (Node `engine-st`: `443 ms` init).
- **Main-thread responsiveness during slicing:** 50 ms main-thread timer fired continuously on every sample (`max_gap_ms = 63–71 ms`, zero blocking).
- **Cache hit:** < 50 ms by canonical SHA-256 input hash (exact G-code match).

| Sample (`web/samples/`) | Tris | Browser slice | Layers (browser / desktop) | Time (browser / desktop) | Grams (browser / desktop) | `gcode.py` lines / part vol |
|---|---:|---:|---:|---:|---:|---:|
| `l_bracket.stl` | 418 | 2.19 s | 200 / 200 | 28.2 / 29.0 min (−2.8 %) | 9.77 / 9.77 g (0.0 %) | 32,560 / 7.78 cm³ |
| `phone_stand.stl` | 44 | 4.27 s | 350 / 350 | 80.0 / 82.7 min (−3.3 %) | 49.74 / 49.66 g (+0.2 %) | 183,140 / 39.99 cm³ |
| `hole_block.stl` | 888 | 3.60 s | 150 / 150 | 28.5 / 29.2 min (−2.4 %) | 11.49 / 11.49 g (0.0 %) | 62,341 / 9.18 cm³ |
| `enclosure.stl` | 1,052 | 7.16 s | 200 / 200 | 65.7 / 65.4 min (+0.5 %) | 37.08 / 37.08 g (0.0 %) | 83,619 / 29.78 cm³ |
| `cable_clip.stl` | 1,656 | 3.11 s | 50 / 50 | 10.1 / 10.2 min (−1.0 %) | 1.56 / 1.56 g (0.0 %) | 18,621 / 1.17 cm³ |

All five G-code outputs parse cleanly in `core/fdmcore/gcode.py` (`tp.slicer == "orca"`, exact layer
counts, part volumes within 0.06 cm³ of desktop OrcaSlicer). The 0.5–3.3 % print-time delta is
expected between desktop OrcaSlicer and `2.5.0-dev` (commit `2d1163eb6f`).

