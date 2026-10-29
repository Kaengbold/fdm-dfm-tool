# Task checklist

Rules: see [AGENTS.md](AGENTS.md). Claim a task by filling in `owner` (name + branch) before
starting. Status: `[ ]` open · `[~]` in progress / partly done · `[x]` done.
Priority: **P1** = needed for a usable public beta, P2 = important, P3 = nice to have.

## Verification (do these first — they decide what's real)

- [ ] **T-001 P1** Check that OrcaSlicer applies the per-object settings in our 3MF (open an export, compare walls/infill/support/brim). Fix `export3mf.py` if not. · files: `core/fdmcore/export3mf.py` · owner: —
- [ ] **T-002 P1** Same check for Bambu Studio and PrusaSlicer (Prusa uses `Metadata/Slic3r_PE_model.config`). · owner: —
- [ ] **T-003 P1** Real-part corpus: collect ~20 typical hobby STLs/STEPs with known good orientations (licence permitting), add to `core/tests/corpus/`, write a regression test for top orientation + key findings. · owner: —
- [~] **T-004 P1** Browser performance on big parts (200k–1M triangles): measure load/orient/check/strength times, fix the worst. · owner: Claude (`task/T-004-performance`) — 572k-face CAD export in Pyodide: load 12→1.6 s, check ~50→16 s, orient 8 s (`node tools/pyodide_check.mjs part.stl`). Fixes: outlines simplified to 0.01 mm, vectorised ring building, no vertex dedupe in placement, `to_bytes()` for uploads. Left: strength/FEA and design timings on big parts, orient (8 s).
- [ ] **T-005 P2** Manual UI review on a laptop screen and a phone; file issues as tasks. · owner: —

## Orientation

- [x] **T-010 P1** Refine the top ~5 orientations with the pseudo-slicer: true first-layer area, supports that land on the part vs. the bed, support volume. · files: `orientation.py`, `api.py` · owner: Claude (`task/T-010-refine`) — `refine.py`; bridges within the material's limit need no support; scars on the part are penalised and named in the reasons. Slicing budget scales with mesh size.
- [x] **T-011 P2** "Critical faces": let the user mark faces that must be flat on the bed / vertical / not touched by supports (cosmetic); use them in the ranking. · owner: Claude (`task/T-011-critical-faces`) — "Keep clean" and "On the bed" (click a face); `faces` in `orientations`/`check`. Left: "must be vertical".
- [x] **T-012 P2** Show *why* an option lost (score breakdown) in the UI. · owner: Claude (`task/T-012-score-breakdown`) — `score_terms` per option (support/height/bed/weak/strength) and a plain `why_lower` line.

## Printability checks & knowledge base

- [~] **T-020 P1** (12 new rules + thresholds now sourced from `research/sourced_*.yaml`; remaining: verify the 404/unreachable links in `research/url_check.txt`, review older rules) Turn `research/fdm_knowledge.md` (unverified) into sourced rules: verify each claim against the sources gathered with `research/RESEARCH_PROMPT.md`, then update `rules.yaml` wording and thresholds. · files: `data/rules.yaml` · owner: —
- [ ] **T-021 P2** New check: embossed/debossed text and small details below printable size (per nozzle). · owner: —
- [ ] **T-022 P2** New checks for fits: press-fit / sliding-fit clearances, pin–hole pairs; suggest tolerances. · owner: —
- [~] **T-023 P2** Threads & fasteners: detect small holes meant for screws, suggest heat-set inserts / nut traps / correct pilot diameters. · owner: Claude (`task/T-026-presets-fasteners`) — holes modelled at M2–M6 nominal size get clearance / pilot / insert sizes (`thresholds.yaml` fasteners). Left: nut traps (hex holes), wall thickness around inserts.
- [x] **T-024 P2** (done as `bridge_sag`) Horizontal hole droop as an *info* finding even when the bridge is short (link to the teardrop edit). · owner: —
- [ ] **T-025 P3** Seam / cosmetic surface hints, ironing suggestion for flat tops. · owner: —
- [~] **T-026 P2** More presets: Creality K1/K2/Ender-3 V3, Elegoo, Anycubic; materials TPU, ASA, PC, PLA+. Cite sources. · files: `data/presets.yaml` · owner: Claude (`task/T-026-presets-fasteners`) — added (Gemini draft, hand-checked); entries marked `verify` in presets.yaml still need a spec-page check; warping lengths for new materials are scaled estimates.

## Findings model (impact-based) — follow-ups

- [x] **T-100 P1** Horizontal holes as features in the checks (diameter, sag, fit). · owner: Claude (`task/T-100-sideways-holes`) — `holes.py` cuts across the hole axis (no scipy needed); closes / undersized / new `sideways_hole_oval` (replaces bridge-sag at the hole). Fit with fasteners → T-022/T-023.
- [~] **T-101 P2** Trapped support in *blind* channels/pockets open on one side (only fully closed cavities are detected now). · owner: Claude (`task/T-101-blind-channels`) — sideways holes too wide to bridge that are blind or longer than 4× their diameter → `trapped_support`. Left: non-round blind pockets/slots.
- [ ] **T-102 P2** Engraved/raised text legibility on top and side faces (stroke width, depth vs nozzle/layer height). · owner: —
- [x] **T-103 P2** Support marks on faces the user marks as visible/critical → raise severity (needs T-011). · owner: Claude (`task/T-011-critical-faces`) — support on a clean face is a *problem* finding with its own box.
- [ ] **T-104 P2** Calibrate warping lengths (`thresholds.yaml`) with real prints per material/enclosure. · owner: —
- [x] **T-105 P2** "Strength" goal: use the voxel simulation automatically when loads are known; heuristics (pins/necks) otherwise. · owner: Claude (`task/T-011-critical-faces`) — the UI sends loads with every re-rank when the goal is Strength.
- [x] **T-106 P3** Shrinkage per material → expected size error for long parts (shape impact). · owner: Claude (`task/T-026-presets-fasteners`) — `shrinkage_pct` per material, `shrinkage_size_error` finding.

## Strength

- [x] **T-030 P2** Several load cases at once (worst case wins). · owner: Claude (`task/T-030-load-cases`) — `case` on forces (UI picker, AI + MCP schemas); one voxel model, one solve per case; all safety factors listed.
- [~] **T-031 P2** Loads on edges/points and "screw here" helper (hole → fixed). · owner: Claude (`task/T-030-load-cases`) — picked hole walls are named ("Ø3.2 mm hole — held by a screw"). Left: loads on edges/points.
- [ ] **T-032 P2** Calibrate layer-strength factors with test prints (CNC-Kitchen-style hooks), document method and results. · owner: —
- [ ] **T-033 P3** Partial-voxel fill fractions for better accuracy on thin sections. · files: `voxel.py`, `strength.py` · owner: —

## Design edits

- [x] **T-040 P2** Chamfer under flat overhangs (replace a flat ceiling edge by 45°). · files: `features.py` · owner: Claude (`task/T-040-design-edits`) — `additions.py`: 45° fill under ledges up to `ledge_max_mm` wide (mitred at corners; under a too-long bridge it makes a pointed arch). Edits now `add` or `cut` material.
- [x] **T-041 P2** Thicken thin walls to 2–3 line widths. · owner: Claude (`task/T-040-design-edits`) — walls under `wall_lines` lines grow on both sides, kept clear of nearby material so gaps don't fuse.
- [x] **T-042 P2** Sacrificial bridge layer for counterbores / holes starting mid-air. · owner: Claude (`task/T-040-design-edits`) — new `hole_starts_midair` finding + one-layer cover edit. Roofs held all round now count as bridges when narrow enough.
- [x] **T-043 P2** Adjustable edit parameters in the UI (chamfer size, teardrop angle). · owner: Claude (`task/T-010-refine`) — `design(params=…)`, inputs in the design panel, MCP `params`. Chamfers on short edges are capped so two don't meet in a new sharp point.
- [ ] **T-044 P2** Feature confirm UI ("is this a hole?", tag ribs/bosses), feeding detection. · owner: —
- [ ] **T-045 P3** Write edits back as STEP (OCCT) instead of STL for CAD users. · owner: —

## Input / output

- [x] **T-050 P2** 3MF input (geometry only, multi-object → pick one). · owner: Claude (`task/T-050-input`) — `formats.py`: build items, components in other model files, units.
- [x] **T-051 P2** Unit sanity check (part 25.4× too small → offer inch → mm). · owner: Claude (`task/T-050-input`) — hint + one-click scale for inch / metre / micron.
- [x] **T-052 P3** Split multi-body STL into separate parts and let the user pick. · owner: Claude (`task/T-050-input`) — cavity shells stay with their body, crumbs dropped.

## AI

- [x] **T-060 P2** OpenAI provider in the Ask-AI panel (BYOK, Responses API, same tools). · files: `web/openai.js`, `web/app.js` · owner: Claude (`task/T-060-openai`) — tested against a mocked API only; real-key test is T-063.
- [ ] **T-061 P2** Local "subscription bridge": in local mode, run the user's official CLI (`claude -p`, `codex exec`, `gemini -p`) headless with our MCP server attached — uses their subscription through the official client. Check each vendor's terms first. · owner: —
- [ ] **T-062 P3** Streaming answers in the Ask-AI panel. · owner: —
- [ ] **T-063 P2** Test the Ask-AI panel with real keys (Anthropic + Gemini), fix tool-schema issues. · owner: —

## Infrastructure & UX

- [~] **T-070 P1** GitHub Actions CI: pytest + e2e smoke test (Windows runner with Edge, or Linux + Chromium). · owner: Claude (`task/T-070-ci`) — `.github/workflows/ci.yml`: pytest, Pyodide check, Linux Chromium e2e. Not run on GitHub yet: verify on the first push/PR (WebGL in headless Linux Chromium is the main risk).
- [ ] **T-071 P2** Hosting: GitHub Pages (needs a public repo or paid plan) or Cloudflare Pages. · owner: —
- [ ] **T-072 P3** Self-host Pyodide/three/manifold/occt assets (offline use, no CDN dependency). · owner: —
- [x] **T-080 P1** Progress indicator + cancel for long operations. · owner: Claude (`task/T-080-progress`) — `progress.py` stages (layers, orientation search, nozzle comparison, FEA iterations); Cancel kills the worker, a new one boots and reloads the part.
- [ ] **T-081 P3** Mobile/tablet layout. · owner: —
- [ ] **T-082 P3** German translation (UI + rules). · owner: —
- [ ] **T-090 P3** Fusion 360 add-in using the same engine. · owner: —

- [x] **T-111 P2** Findings UI: an impact group "Will fail" can contain a TIP-severity card (e.g. small bed contact that a brim fixes), which reads as a contradiction. Show the severity the group implies, or move fixed-by-setting items to a "needs a setting" note. · owner: Claude — group renamed "Can fail"; the card's severity says how likely.

## Done

- [x] Fixed a T-110 regression: grid-snapping layer outlines crashed Pyodide's GEOS on the L-bracket; now only invalid outlines are repaired (`tools/pyodide_check.mjs` added).
- [x] **T-110 P1** Fix GEOS topology exception in Pyodide on real models; dynamic goal presets (walls/infill/layer height); camera focus & 3D highlight on clicked findings; orientation box clearing; WIP yellow frames on unfinished features. · owner: Antigravity (branch `task/T-110-ux-fixes`)
- [x] Phase 0–2: engine, Pyodide app, orientation, pseudo-slicer checks, knowledge base, settings, 3MF/STL export, voxel FEA strength, strength-aware orientation.
- [x] Phase 3 (partial): hole detection, teardrop + corner chamfer edits with ghost preview.
- [x] Phase 4: MCP server, copy-report, Ask-AI panel (Anthropic + Gemini, own key).
- [x] STEP import with CAD faces; build-volume check / turn-to-fit.
- [x] Orientation fix for complex parts (strong penalty for no/tiny bed contact; hide bad options); typical-part regression tests.
- [x] Browser smoke test (`tools/e2e_smoke.py`) with optional Gemini screenshot review.
- [x] Impact-based findings (fails / shape / surface / weak / info): supports and overhangs no longer reported on their own; new checks: warping by material+enclosure, holes that close or print undersized, gaps that fuse, first-layer detail, rough fine detail, upright pins, necks, trapped support, bridge sag. Thresholds in `data/thresholds.yaml`.
- [x] Setup: multi-material option, goal (balanced/strength/speed/material), nozzle comparison; findings grouped by impact.
