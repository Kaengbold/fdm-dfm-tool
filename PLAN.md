# FDM DFM Tool — Plan & Requirements

Working title: **FDM DFM Tool** (rename later)
Status: scoping · 2026-10-28

## 1. Vision

A free, open-source "design for FDM" assistant for hobbyists. Drop in a part, get:
the best print orientation, the right slicer settings, warnings about what will fail,
and (if you can edit the design) concrete, previewable geometry fixes.
It works by just opening a website. AI is an optional extra, never a requirement.

## 2. Guiding principles

1. **Zero install, zero account.** Open the URL, drop a file, get results. No server needed for the core.
2. **Plain language.** No FEA jargon in the default UI. "This hook will probably snap along the layers" instead of "σ_zz = 23 MPa".
3. **Code first, AI second.** Every check runs as deterministic code. AI reviews, explains, and proposes; the code validates.
4. **Everything is an API.** Whatever the UI can do, a script or an AI agent can do too.
5. **Rough but honest.** Simulations are indications, labelled as such.
6. **Single part only.** No assemblies.

## 3. Users & modes

Primary user: hobbyist with a consumer printer (Bambu, Prusa, Creality, Voron…), mostly OrcaSlicer/Bambu Studio/PrusaSlicer.

Mode toggle (every suggestion carries a domain tag `geometry` / `slicer` / `process`):

| Mode | For | Shows |
|---|---|---|
| **Just print it** (default) | Downloaded STL/3MF, can't edit | Orientation, slicer settings, process tips |
| **I'm designing it** | Own CAD model, someone else slices | Geometry fixes only + handoff notes |
| **Both** | Own design, own printer | Everything |

## 4. Architecture

```
Browser (static site, e.g. GitHub Pages — no backend)
├── UI: React + three.js (react-three-fiber)
├── Geometry I/O (JS/WASM): opencascade.js/replicad (STEP), manifold-3d (booleans), 3MF/STL parsers
└── Core engine: Python package `fdmcore` running in Pyodide (Web Worker)
      numpy · scipy · trimesh · shapely

Same `fdmcore` package, also usable as:
├── Local mode:  `pipx install fdmdfm` → local server + same UI + MCP server + OrcaSlicer CLI
└── Fusion add-in (later): Fusion bundles Python → same package, exact B-rep features
```

Key decisions:
- **Browser-first via Pyodide.** Python for the engine (easy to contribute to), but runs in the browser, so hosting is free and files never leave the user's machine.
- **Voxel FEA instead of tet meshing.** Regular hex grid aligned with the build direction: no meshing failures on bad STLs, shell/infill zones fall out of distance-to-surface, layers map directly to voxel slabs. Target 100k–300k DOF ≈ 3–15 s in browser.
- **Own "pseudo-slicer" in core** (layer areas, overhang per layer, bridges, layer-time estimate). Good enough for advice; real slicing via OrcaSlicer CLI in local mode only.
- **Output to the user's slicer:** export a 3MF with orientation + Orca/Bambu project settings → "Open in OrcaSlicer" and everything is set.
- **Not forking OrcaSlicer.** No plugin system, huge C++ codebase. Integrate via 3MF export, profile import, CLI.

### Internal model (STL and STEP unified)
- **Mesh** — always present; used for overhang, thickness, voxelization.
- **Feature graph** — face regions with fitted primitive (plane/cylinder/cone/…), semantic tag (hole, boss, rib, fillet, text…), confidence, user tags (critical face, load face, fixed face).
  - STEP: exact from B-rep. STL: segmentation + primitive fitting, with confidence.
  - Low confidence → ask the user ("Is this a hole?") or let AI review.

## 5. Requirements

### Must (v1)
- [ ] Load STL, 3MF, STEP (single body; reject/warn on multi-body)
- [ ] Mesh repair check (non-manifold, inverted normals) with auto-fix
- [ ] Feature graph with confidence + "please confirm" prompts
- [ ] Printer/material/nozzle presets (simple picker: "Bambu P1S, PLA, 0.4")
- [ ] Orientation optimizer with an explained score (supports, bed contact, critical faces, height, strength)
- [ ] Pseudo-slice analysis: overhang map, bridges, thin walls, small bed contact, tall/thin features, small-layer-time hotspots
- [ ] Rule engine + knowledge base (YAML), ~20 rules at launch
- [ ] Mode toggle (print / design / both)
- [ ] Slicer settings recommendation (walls, infill, brim, supports, cooling) with reasons
- [ ] Export 3MF with orientation + settings for OrcaSlicer/Bambu Studio; plain STL export
- [ ] Results are shareable as a report (Markdown/PDF)
- [ ] Fully functional without AI, offline-capable after first load

### Must (v2 — strength)
- [ ] Simple load definition: click face(s) → "fixed" / "pushed/pulled with X kg" (kg, not Newtons by default)
- [ ] Voxel FEA, transversely isotropic material with Z knockdown, shell (walls) + homogenized infill core
- [ ] One solve → rotate stress field → score all orientations for layer-interface stress
- [ ] Traffic-light result: "fine / borderline / likely to break" + where and why
- [ ] Presets affect Z strength (heated chamber, enclosure, material)
- [ ] Suggest walls/infill/orientation to reach "fine"

### Should
- [ ] Ghost preview of geometry fixes (teardrop/diamond holes, chamfer bed edges, add ribs, thicken walls) using manifold-3d; export modified STL/STEP
- [ ] A/B compare of two orientations or settings
- [ ] AI: bring-your-own API key (Anthropic/OpenAI called directly from the browser, key stays local)
- [ ] AI: "copy context for chat" — export an analysis bundle to paste into any chat (works with any subscription, zero setup), paste back a JSON answer
- [ ] Local mode with MCP server (Claude Code / Codex / Claude Desktop operate the tool with the user's own subscription)
- [ ] Local mode: real slicing via OrcaSlicer CLI + Orca profile import

### Later
- [ ] Fusion 360 add-in (exact features, apply fixes as real CAD features)
- [ ] Hosted remote MCP endpoint (for Claude.ai connectors) if there's demand
- [ ] Community-contributed rules & printer presets
- [ ] Calibration from real test prints (tune knockdown factors)

### Out of scope
- Assemblies, multi-material planning, accurate quantitative strength, warping/thermal simulation beyond layer-time heuristics.

## 6. AI integration (optional layer)

Never use consumer subscription logins inside the app (against Anthropic/OpenAI ToS). Allowed paths:

| Path | Setup for user | Notes |
|---|---|---|
| Copy/paste context bundle | None | Works with any chat subscription |
| BYOK API key in browser | Paste a key | Cheap model (Haiku-class) is enough for feature review |
| MCP (local mode) | Install local mode, connect agent | User's own Claude Code/Codex subscription; full agent control |

MCP / API tools: `load_part`, `list_features`, `tag_feature`, `set_mode`, `set_preset`, `set_load_case`,
`run_orientation`, `run_strength`, `list_issues`, `propose_edit`, `evaluate_edit`, `export_3mf`.
AI loop: agent proposes → engine re-runs checks/simulation → result returned → agent iterates.

## 7. Knowledge base format

One YAML file per rule, in git, community-editable:

```yaml
id: horizontal-hole-overhang
title: Horizontal hole will sag at the top
detect: feature.type == hole and angle(feature.axis, Z) > 60 and feature.diameter > 6
severity: warn
domain: [geometry, slicer]
explain: The top of a horizontal hole is an unsupported overhang ...
fixes:
  - domain: geometry
    op: teardrop_hole
    params: {angle: 45}
  - domain: slicer
    settings: {support_type: tree, support_on_build_plate_only: true}
images: [teardrop.png]
sources: []
```

Launch rule set (draft): sharp bed edges · thin walls < 2 lines · overhangs > 50° · long bridges ·
horizontal holes (teardrop) · sacrificial bridging for counterbores · small bed contact (brim/ears) ·
tall thin features · layer-time hotspots · text/emboss min size · press-fit tolerances ·
Z-loaded clips/hooks/bosses · thread suggestions (heat-set inserts) · nozzle size suggestion ·
"ribs instead of supports".

## 8. Material model (starting values, user-adjustable)

| Material | Z/XY strength (open) | Heated chamber |
|---|---|---|
| PLA | 0.4–0.6 | n/a |
| PETG | 0.5–0.75 | small gain |
| ABS/ASA | 0.2–0.4 | 0.6–0.75 |
| PA-CF | 0.15–0.3 | gain |

Infill stiffness: Gibson–Ashby scaling `E* ≈ E·(ρ)^n`. Walls: shell thickness = walls × line width at full XY properties.

## 9. Tech stack & licensing

- License: **AGPL-3.0 or GPL-3.0** for the project (lets us reuse GPL/AGPL tools freely; keeps forks open). Decide before first commit.
- Python core: numpy, scipy, trimesh, shapely (all Pyodide-compatible)
- Browser: React, three.js/react-three-fiber, Pyodide, opencascade.js/replicad, manifold-3d
- Local mode: FastAPI, MCP Python SDK, OrcaSlicer CLI (subprocess)
- Tests: pytest on a corpus of reference parts (brackets, hooks, enclosures, Benchy…)

## 10. Phases

| Phase | Goal | Deliverable |
|---|---|---|
| **0 — Spikes** (1–2 wks) | Kill the risks | Pyodide + trimesh in a web worker; STEP via opencascade.js → mesh + face types; voxel FEA prototype on a bracket; 3MF export opens in Orca with settings |
| **1 — "Just print it" MVP** | Useful on day one | Static site: upload, presets, orientation, pseudo-slice checks, ~20 rules, settings, 3MF export |
| **2 — Strength** | Hobbyist-friendly loads | Click-to-load UI, voxel FEA, stress-aware orientation, traffic-light results |
| **3 — Design fixes** | "I'm designing it" mode | Feature graph confirm UI, ghost edits, modified STL/STEP export |
| **4 — AI & agents** | Optional superpowers | Copy-context, BYOK, local mode + MCP + Orca CLI |
| **5 — Ecosystem** | Grow | Fusion add-in, community rules/presets, calibration prints |

### Progress (2026-10-29)

| Phase | Status | Notes |
|---|---|---|
| 0 — Spikes | ✅ done | Pyodide engine, orientation, overhang map, STEP import (occt-import-js, exact CAD faces) |
| 1 — "Just print it" | ✅ done | Presets (16 printers, 8 materials), pseudo-slicer checks (~27 rules), STL/3MF/STEP input with multi-body pick and unit hints, settings, 3MF/STL export, report, progress + cancel |
| 2 — Strength | ✅ done | Face regions, voxel FEA, anisotropic material, strength-aware orientation, load cases, loads on faces or spots |
| 3 — Design fixes | ✅ done | Feature confirm ("is this a hole?"), teardrops, corner chamfers, 45° ledge fills, sacrificial layers, thicker walls; ghost preview (cut/add), adjustable sizes, STL and STEP output (OpenCascade write-back) |
| 4 — AI & agents | ✅ done | MCP server (local mode) with real slicing via the installed OrcaSlicer, copy-report + paste-back answers (any chat subscription), Ask AI with Anthropic / Gemini / OpenAI keys. Open: real-key tests (T-063), streaming (T-062), CLI subscription bridge (T-061) |
| 5 — Ecosystem | ⏳ | Fusion add-in, community presets/rules, calibration prints, hosting |

Research notes: `research/fdm_knowledge.md` (Gemini, unverified) — source for future rules.

Known gaps: UI only verified by syntax checks + engine runs under Pyodide/Node (needs a real browser pass);
3MF per-object settings need confirming in OrcaSlicer/Bambu Studio; print-time model is rough; voxel FEA
misses features thinner than a voxel (~1 mm).

## 11. Launch presets

Printers (Bambu + Prusa first):

| Printer | Enclosed | Notes |
|---|---|---|
| Bambu A1 / A1 mini | No | ABS/PA-CF: warn (warping, weak Z) |
| Bambu P1S / X1C / P2S / H2D | Yes (passive/active) | Good for ABS; X1C/H2D hardened for PA-CF |
| Prusa MK4S / MINI+ | No | ABS: recommend enclosure |
| Prusa Core One | Yes | Good for ABS/PA-CF (with hardened nozzle) |

Materials: **PLA, PETG, ABS, PA-CF**.
- ABS: enclosure strongly recommended; open-frame → lower Z factor + warping warnings, brim.
- PA-CF: needs hardened nozzle + dry filament; warn otherwise; strongest anisotropy (Z ≈ 0.15–0.3).

## 12. Open questions
- Project name & license (leaning AGPL-3.0)
- How to source/validate knockdown factors (literature + own test prints?)
