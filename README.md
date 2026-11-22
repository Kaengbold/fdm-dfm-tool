# FDM DFM Tool

A free, open-source **design-for-FDM** assistant for hobbyists. Drop in a part and get
the best print orientation, the right slicer settings, warnings about what will fail,
a rough "will it hold?" strength check, and suggested design fixes — all in the
browser, no install, your files never leave your computer.

> **Status: pre-alpha.** See [PLAN.md](PLAN.md) for the roadmap.

## What it does

One guided flow on a desktop browser (1280 × 720 or larger), with the part in 3D the whole way:

| Step | |
|---|---|
| **Your part** | STL, 3MF or STEP (exact CAD faces). Files with several parts let you pick one; parts exported in inches or metres get a one-click rescale. Saved projects and shared reports open here too |
| **Setup** | Your printer (presets, or any printer OrcaSlicer knows), the nozzles you have, a material or "decide later", the goal (Strong, Balanced, Fast, Light or Quality) and what may change: slicer settings, the design, or both |
| **Environment** | How warm it gets in use and what it touches (water, food, sunlight, oil…), so unsuitable materials are left out |
| **Holds and loads** | Click on the part: where it is held (a face, an edge, an inside corner, a screw hole) and where it is pushed or pulled (in N or MPa, aimed with a 3D arrow); then how long the load stays and whether it shakes |
| **Analyse → Options** | The optimizer tries orientations, materials, nozzles, layer heights, walls, top/bottom layers and infill (at Complete also local reinforcements) and checks each candidate's strength at its own pose with a voxel FEA that knows the weaker layer bonds. You get 2–4 options side by side against the goal's safety target (Strong ×5, Balanced ×3, Fast ×2, Light ×1.5); pin a value you want and re-optimize |
| **Simulation** | OrcaSlicer, running in your browser, slices the chosen option in the background (or drop in your own G-code): heat per spot and layer cooling, warping (true size or exaggerated), internal stress and overhangs on the part |
| **Problems & fixes** | Every problem with its fixes first: a slicer setting, a design change previewed on the part (teardrop holes, chamfers, thicker walls…), a printing tip, or the number to change in CAD. Collect fixes in one batch, confirm, and the part is changed safely and checked again (problems, strength, a better orientation if there is one); one click undoes the batch |
| **Done** | A 3MF for OrcaSlicer, Bambu Studio or PrusaSlicer with the part turned and every setting you ticked; the changed part as STL (a changed STEP part comes back as STL for now); G-code from the browser OrcaSlicer; a project file, a report to share and a CAD checklist |

The **Quality** goal is for parts that must look good: it skips holds, loads and strength, guesses which faces people
see (the outer ones) and keeps support off them; you can mark the face that matters instead (agents:
`guess_visible_faces`, then `faces` in `optimize_print`).

**Settings & calibration** (from the header at any time): your printers and materials, imported slicer profiles, and
small test prints that measure your printer (layer strength, warping, fits, overhang angle, bridges).

**AI agents:** no AI inside the app. Your own agent (Claude Code, Codex, Claude Desktop) operates the same engine
through the MCP server with *your own* subscription, including the app's optimizer (`optimize_print`), and can write
the report as HTML or Markdown with pictures and charts. See [Use it from an AI agent](#use-it-from-an-ai-agent-local-mode).

Autodesk Fusion users: an add-in in [`fusion/`](fusion/README.md) runs the same checks on a body and can add teardrops as real CAD features (early, not yet tested inside Fusion).

Presets: Bambu Lab A1 / A1 mini / P1S / X1C / H2D, Prusa MK4S / MINI+ / CORE One / XL, Creality K1C / K2 Plus / Ender-3 V3 SE / KE, Elegoo Centauri Carbon, Anycubic Kobra 3 · PLA, PLA+, PETG, ABS, ASA, PC, TPU 95A, PA-CF · 0.2–0.8 mm nozzles.

## Where it runs and what it costs

In short:

- Everything is worked out in your web browser, on your own computer. Your part files are never uploaded anywhere.
- The app has no AI built in and asks for no account and no API key.
- The site is only static files. Anyone can host it for free (for example on GitHub Pages); there is no server that does the work, so there are no server costs, only the bandwidth for the files.
- On your first visit, your browser downloads the Python runtime the engine runs in (Pyodide, about 15 MB). The strength and design tools download one more package (scipy, about 10 MB) the first time you use them.
- Where these come from: the published site serves these files itself. When you run it locally with `python -m http.server`, they come from the public jsDelivr CDN instead.
- After the first visit, the app keeps what it downloaded, so it starts faster and also works offline.
- Your own computer does the calculating, so big parts or long simulations (strength, warping) can take a while and keep your computer busy. You can cancel a long run at any time.

## Try it locally

No build step. From the repository root:

```sh
python -m http.server 8000
```

Then open <http://localhost:8000/web/>. What it downloads on the first visit: see [Where it runs](#where-it-runs-and-what-it-costs).

To publish it, enable GitHub Pages with source "GitHub Actions": `.github/workflows/pages.yml`
deploys `web/` and the engine on every push to `main`.

## Use it from an AI agent (local mode)

Two commands, no download of this repository. You need [uv](https://docs.astral.sh/uv/) (or pipx) and your
agent (Claude Code, Codex or Gemini CLI):

```sh
uv tool install "fdmdfm[local] @ git+https://github.com/Kaengbold/fdm-dfm-tool"
fdmdfm setup
```

`fdmdfm setup` finds your agents and asks before each change: it adds the MCP server to Claude Code, Codex and
Gemini CLI, and copies the guide for Claude Code. Restart the agent and ask it to check a part. With pipx
instead of uv: `pipx install "fdmdfm[local] @ git+https://github.com/Kaengbold/fdm-dfm-tool"`.

`fdmdfm open part.stl` opens the app in your browser with the part, served from your computer (at
<http://127.0.0.1:8771/web/>; keep the window open while you use it, Ctrl+C stops it). Everything the tool
needs comes with it: the printer and material data and the slicer profiles, whatever you have installed.

From a checkout (for developers), or to set the agents up by hand:

```sh
pip install -e "core[local]"
claude mcp add fdm-dfm -- python -m fdmcore.mcp_server
```

For Codex, add to `~/.codex/config.toml`:

```toml
[mcp_servers.fdm-dfm]
command = "python"
args = ["-m", "fdmcore.mcp_server"]
```

For Gemini CLI, add to `~/.gemini/settings.json`:

```json
{ "mcpServers": { "fdm-dfm": { "command": "python", "args": ["-m", "fdmcore.mcp_server"] } } }
```

By default the agent may read and write files anywhere you can. To keep it inside one folder, add
`--folder C:\path\to\parts` after `fdmcore.mcp_server` (or set `FDM_DFM_FOLDER`). It never replaces a
file it didn't write itself in that session; set `FDM_DFM_OVERWRITE=1` to allow that.

Then give the agent the guide (how to use the tools, the units, how to explain the results):

- Claude Code: copy `agents/fdm-check/` to `~/.claude/skills/` (or to `.claude/skills/` in your project).
- Codex and others: paste `agents/AGENTS-snippet.md` into your project's `AGENTS.md`.

Project files: the agent saves its work (`save_project`: part, orientation, loads, zones, boxes, settings) as a `.json` you drop into the web app to see it in 3D; "Save project" in the app writes the same file for the agent (`open_project`, or `fdmdfm check project.json`).

With OrcaSlicer installed (any version, any printer in our list), the agent can also slice for real (`slice_with_orcaslicer`: OrcaSlicer's own print time and filament use). The printer, process and filament profiles for that come with this tool, so it doesn't depend on how your OrcaSlicer stores its own.

This is the supported way to use a Claude, ChatGPT or Gemini *subscription* with the tool:
the official client (Claude Code, Codex, Gemini CLI) runs the model, our MCP server only
provides the tools. The web app itself never uses subscription logins.

No MCP? The same engine as a command line (JSON out, errors as one line on stderr):

```sh
fdmdfm check part.stl --material petg          # findings + settings, best orientation
fdmdfm faces part.stl                          # face ids for loads
fdmdfm strength part.stl --loads loads.json    # safety factor
fdmdfm export part.stl -o part.3mf             # 3MF for your slicer
fdmdfm report part.stl -o report.html          # report with pictures and charts (.md works too)
fdmdfm chart part.stl --kind nozzles -o t.svg  # chart: orientations, nozzles, warping, heat
fdmdfm render part.stl --view overhangs -o under.png   # picture (bed, overhangs, problem, stress, warp, compare)
```

Then ask e.g. *"Load ~/parts/bracket.stl, find the best orientation for PETG on my P1S,
check it holds 5 kg on the top face, apply sensible design fixes and export a 3MF."*

## Layout

| Path | What |
|---|---|
| `core/fdmcore/` | Analysis engine (Python). Runs natively and in the browser via Pyodide |
| `core/fdmcore/data/` | Presets and the knowledge base (`rules.yaml`) — contributions welcome |
| `core/tests/` | `pytest` tests |
| `web/` | Static web app (`index.html`, `main.js`): the shell and steps in `steps/`, components in `ui/`, the 3D view in `viewport/`, the Pyodide worker, manifold-3d booleans, the browser slicer |
| `web/samples/` | Demo parts (generated by `tools/make_samples.py`) |
| `tools/` | Dev scripts (`make_samples.py`, `make_manifest.py`, `e2e_smoke.py`) |
| `research/` | Background notes and the research prompt (unverified unless stated) |

Working with several people or AI agents? Read [AGENTS.md](AGENTS.md) and pick a task from [TASKS.md](TASKS.md).

## Development

```sh
cd core
pip install -e ".[dev]"
python -m pytest
python ../tools/make_manifest.py   # after adding/removing engine files
cd .. && python tools/e2e_smoke.py # browser test: every step, then the whole journey at 1280×720 and 1440×900 (pip install playwright; uses installed Edge)
```

Engine modules used by the web app must only need numpy, shapely, pyyaml or scipy
(all available in Pyodide). Local-only modules (`mcp_server.py`, `booleans.py`) are
excluded from the web manifest.

## Accuracy

Everything here is an *indication*, not a certification: the pseudo-slicer ignores
toolpaths, the strength model is a coarse voxel FEA with typical material values, and
print time/filament are rough. Use it to make better decisions, not to sign off parts.

## License

[AGPL-3.0-or-later](LICENSE). Uses three.js (MIT), Pyodide (MPL-2.0), shapely (BSD),
manifold-3d (Apache-2.0), occt-import-js and replicad-opencascadejs / OpenCascade (LGPL-2.1),
and the [Muon3D Slicer Engine](https://github.com/Muon-3D/muon3d-slicer-engine) (AGPL-3.0-only,
WebAssembly build of OrcaSlicer; fetched and verified by `tools/vendor.py` with its upstream
`LICENSE`, `NOTICE`, `SOURCE.md` and `THIRD-PARTY-NOTICES.md`). The Atkinson Hyperlegible font
(SIL OFL 1.1, Braille Institute) ships in `web/fonts/`. OrcaSlicer's printer / process / filament
profiles (AGPL-3.0, from its source at the version named inside) ship flattened in
`core/fdmcore/data/orca_profiles.json.gz`. The beds, heights and nozzles of the other printers
OrcaSlicer has profiles for (same licence and version) are in
`core/fdmcore/data/orca_printers.json`, for "My printer isn't listed".
