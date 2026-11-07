# FDM DFM Tool

A free, open-source **design-for-FDM** assistant for hobbyists. Drop in a part and get
the best print orientation, the right slicer settings, warnings about what will fail,
a rough "will it hold?" strength check, and suggested design fixes — all in the
browser, no install, your files never leave your computer.

> **Status: pre-alpha.** See [PLAN.md](PLAN.md) for the roadmap.

## What it does

| | |
|---|---|
| **Input** | STL, 3MF or STEP (exact CAD faces). Files with several parts let you pick one; parts exported in inches or metres get a one-click rescale |
| **Orientation** | Ranks build directions by supports (sliced: bridges, supports landing on the part), bed contact, height — and strength if you add loads; click faces to keep them clean of support marks or put them on the bed; says why other options rank lower; checks the build volume and turns the part to fit |
| **Printability** | Pseudo-slices the part: overhangs vs. bridges, thin walls, sharp bed corners, tipping, slender features, heat build-up (small pins and tips that are still soft when the next layers go on, with a heat colour view), material/printer issues |
| **Explanations** | Every finding has a plain-language *why* and fixes, filtered by what you can change: *just print it* / *the design* / *both* |
| **Slicer settings** | Recommended OrcaSlicer / Bambu Studio settings, exported in a 3MF together with the oriented part |
| **Strength** | Click a face → "held here" or "2 kg pulling". Voxel FEA with weaker layer bonds (material + enclosure dependent) with your walls, top/bottom layers and infill gives a traffic light, a heat map and a plain explanation; "Find the lightest settings that hold" searches walls, layers, infill and orientation for your load |
| **Detailed check** | Load the G-code you sliced from the exported 3MF (OrcaSlicer, Bambu Studio incl. `.gcode.3mf`, PrusaSlicer, Cura): strength then uses the plastic really printed in each spot and which way the lines run (lines pulled apart sideways are weak), and the heat check uses the real layer times. Locally, the MCP tool `detailed_strength` slices with the installed OrcaSlicer itself |
| **Design fixes** | Detects holes and round posts and asks when unsure ("is this a hole?"); suggests teardrop holes, corner chamfers, 45° fills under ledges, sacrificial layers over holes that start in mid-air and thicker walls; previews them (red = cut, green = added), applies them and re-checks; STEP parts are written back as STEP with only the edited spots changed |
| **AI agents** | No AI inside the app. Your own agent (Claude Code, Codex, Claude Desktop) operates the tool through the MCP server with *your own* subscription; it can write the report as HTML or Markdown with pictures and charts (`write_report`, `render_image`, `make_chart`, or the `fdmdfm` command line). Or copy the report into any AI chat |

Add your own printer or filament (or import an OrcaSlicer filament profile) and measure it with small test prints: layer strength, warping, fits (snug / sliding gaps), overhang angle and bridge length.

Autodesk Fusion users: an add-in in [`fusion/`](fusion/README.md) runs the same checks on a body and can add teardrops as real CAD features (early, not yet tested inside Fusion).

Presets: Bambu Lab A1 / A1 mini / P1S / X1C / H2D, Prusa MK4S / MINI+ / CORE One / XL, Creality K1C / K2 Plus / Ender-3 V3 SE / KE, Elegoo Centauri Carbon, Anycubic Kobra 3 · PLA, PLA+, PETG, ABS, ASA, PC, TPU 95A, PA-CF · 0.2–0.8 mm nozzles.

## Try it locally

No build step. From the repository root:

```sh
python -m http.server 8000
```

Then open <http://localhost:8000/web/>. The first visit downloads the Python runtime
(Pyodide, ~15 MB); strength and design tools fetch scipy (~10 MB) when first used. After that the
app also works offline (a service worker keeps what it downloaded).

To publish it, enable GitHub Pages with source "GitHub Actions": `.github/workflows/pages.yml`
deploys `web/` and the engine on every push to `main`.

## Use it from an AI agent (local mode)

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

With OrcaSlicer installed, the agent can also slice for real (`slice_with_orcaslicer`: OrcaSlicer's own print time and filament use).

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
```

Then ask e.g. *"Load ~/parts/bracket.stl, find the best orientation for PETG on my P1S,
check it holds 5 kg on the top face, apply sensible design fixes and export a 3MF."*

## Layout

| Path | What |
|---|---|
| `core/fdmcore/` | Analysis engine (Python). Runs natively and in the browser via Pyodide |
| `core/fdmcore/data/` | Presets and the knowledge base (`rules.yaml`) — contributions welcome |
| `core/tests/` | `pytest` tests |
| `web/` | Static web app: three.js viewer, Pyodide worker, manifold-3d booleans |
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
cd .. && python tools/e2e_smoke.py # browser smoke test (pip install playwright; uses installed Edge)
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
manifold-3d (Apache-2.0), occt-import-js and replicad-opencascadejs / OpenCascade (LGPL-2.1), Anthropic TypeScript SDK (MIT). The Atkinson Hyperlegible font (SIL OFL 1.1, Braille Institute) ships in `web/fonts/`.
