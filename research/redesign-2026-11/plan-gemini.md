# UI & Workflow Redesign Plan (Gemini 4 Neon) — Feedback Round 5

**Source of truth:** [`feedback.md`](feedback.md) (verbatim transcript, 2026-11-21).
**Reviewed draft:** [`plan-claude.md`](plan-claude.md).
**Constraints respected:** Pyodide engine (`numpy`, `scipy`, `shapely`, `pyyaml` + stdlib only), no build step, plain ES modules, plain English for hobbyists, no built-in AI, every engine API gets an MCP tool (`test_every_api_method_has_a_tool`).

---

## 1. Review of Claude's Draft

Claude's draft captures the big architectural pivot well (single desktop-only guided flow, spatial 3D interactions at the cursor, autonomous multi-variable optimizer, column options with constraints, pending design fixes with confirm-all). However, checking it line-by-line against `feedback.md` and the real engine code in `core/fdmcore/` exposes several misses, technical flaws, and phase-ordering mistakes.

### 1.1 What Claude's draft misses or gets wrong versus `feedback.md`

1. **Start screen entry for Settings & calibration:**
   - Claude leaves this as Open Question 1 ("header gear only, or also a third (smaller) entry on the Start screen?") and only puts a tiny text link at the bottom of the Start screen.
   - **What the feedback actually says:** Early on, the owner starts with two main windows (*"Optimize your print"* and *"Fix your broken prints — Coming soon"*), then explicitly adds near the end: *"Also, just remembering we, we had a lot of calibration tests in the, in the first, um, first, first view where you have like, um, optimize print, fix print problems, and then we have like settings. Settings and calibrations, and there we have all the things he can put in."*
   - **Fix:** Give Settings & calibration a proper, visible third entry on the Start screen (a clean full-width secondary card directly beneath the two primary workflow cards) **plus** the header gear icon so it is reachable from any step.

2. **Options screen (Step 8) — 40-checkbox UI clutter:**
   - Claude puts a per-row `"Keep"` checkbox **and** inline dropdown/number editor in **every column** (`4 columns × ~10 rows = 40 checkboxes + 40 inputs`). That directly recreates the *"mess of buttons"* the owner hated, and creates ambiguity if a user ticks Orientation in Column 1, Material in Column 3, and edits Walls in Column 2.
   - **What the feedback actually says:** *"he has like these columns, let's say makes four options... you can like accept all of them, or accept some of them, and then you click one, two, and then say, I want this one, or you change then values, and say okay, and then there's a button... 'Optimize with my constraints'"*.
   - **Fix:** Keep the columns visually clean and scannable. Clicking a pin icon on any cell (or clicking a value to edit it) adds/updates a chip in a single **My constraints** bar below the columns (e.g., `[Material: PLA ×] [Nozzle: 0.4 mm ×] [Re-optimize with my choices]`). Zero matrix of 40 checkboxes.

3. **No path for non-loaded parts on Balanced / Fast / Light:**
   - Claude hard-blocks Step 4 (Holds) and Step 5 (Loads) with `Next enabled after ≥ 1 hold` and `Next needs ≥ 1 load`, allowing strength to be skipped **only** when the goal is `Quality`.
   - Many hobbyists pick **Balanced**, **Fast**, or **Light** for a tray, stand, or organizer that carries no real load. Forcing them to invent a fake hold and fake force breaks beginner use case #1 in `PRODUCT.md`.
   - **Fix:** Provide a quiet secondary action on Step 4 (`"No mechanical load — skip strength"`) for Balanced, Fast, and Light (not Strong), OR confirm with the owner whether non-loaded parts should always use Quality (see Open Question 2).

4. **Inside corners in Step 4 (Holds):**
   - **What the feedback says:** *"it first highlights every time you hover over an area or like an edge, the specific area. You click on it, and then, um, it pops up, 'Do you want to fix that specific area?' Yes, um, then it's fixed. Nice. The same if you have some inside corners, everything fine."*
   - Claude's radial menu only lists `"Hold this face"` / `"Hold this edge"` / `"Cancel"` and omits inside corners, even though `features.concave_edges` already exists in the engine.

5. **Optional re-check after "Confirm all changes" (Step 10):**
   - **What the feedback says:** *"he applies them all, we do the recalibration of everything, um, or we ask if we should recheck everything, if not, it's fine, or here again the options, only do the, the minimum... we do the strength analysis again, do we have still kind of the same safety factor? Nice. That's it."*
   - Claude makes a 3-stage re-check mandatory with no choice. Giving a clean default (`"Apply & quick re-check"`) with an inline secondary option (`"Apply without re-checking"`) matches the feedback directly.

6. **Step 9 ("Print simulation") is an empty 1-button dead end in Claude's draft:**
   - Claude has a whole wizard step containing essentially one button (`"Run simulations"`), forcing an extra click right after `"Use this option"` in Step 8.
   - **Fix:** Make Step 9 genuinely useful: show the chosen option's print conditions (room temp, bed temp, enclosure, plus optional sliced G-code upload for detailed thermal until browser slicing exists), run the simulation with live stage progress, and let the user inspect the resulting 3D Heat / Warp / Residual Stress overlays right there before moving to Step 10 (Problems & fixes).

7. **Part loading edge cases ignored (Step 1):**
   - Claude writes *"On load: the part shows in the stage and the flow advances automatically to Setup."*
   - Existing engine features (`T-050`, `T-051`, `T-052`, `T-459`) detect **multi-body files**, **inch/cm/m unit mistakes**, and **broken meshes (open edges / flipped normals)**. Auto-advancing blindly on a multi-body STL or a 25.4× too-small part would break them. Step 1 must pause auto-advance for multi-body selection and show a clean inline action banner for unit rescaling or mesh damage.

8. **Visual direction clings too closely to the old palette:**
   - Claude writes *"keep DESIGN.md tokens as a starting palette"*. The owner explicitly warned: *"The current UI is dog shit. Like, don't take inspiration on the current UI, especially not how it's written, how it's made."* We should build a genuinely crisp, modern desktop CAD workbench visual language from scratch (neutral slate surfaces, sharp 1 px structure, high-contrast focal accents), not carry forward the old green-tinted styling by inertia.

---

### 1.2 Technical risks, wrong assumptions, and engine mismatches in Claude's draft

1. **Major unflagged expectation gap — browser OrcaSlicer (`T-127`) does NOT exist yet:**
   - In `feedback.md`, the owner assumes we already have an internal browser slicer: *"because we can already use an internal slicer, um, which we already had, like the OrcaSlicer, we use the same slicing engine to get a sliced result so we can directly use the G-code... and then you get straight the G-code."*
   - **Repo reality (`TASKS.md` T-127 & T-483):** OrcaSlicer CLI only runs in **local mode** (`fdmdfm` / `slicer_cli.py`). The browser WASM build (`Muon3D engine-st.wasm` v0.6.1, ~11 MB WASM + ~10 MB profiles, AGPL-3.0-only pre-release) was **postponed on 2026-11-07** and is not built. Furthermore, the Thermal Optimizer tasks (`T-472` kinematics, `T-473` thermal tier A, `T-474` window, `T-476` heat→strength, `T-477` real print go/no-go, `T-480` optimizer, `T-481` G-code rewrite) are all still **open (`[ ]`)**.
   - Claude glosses over this in one parenthetical. We **must** flag this honestly to the owner and design Step 9 & Step 11 with a clear two-tier contract:
     - **Works in the browser on Day 1:** pseudo-slice `heat.py` (1D layer cooling) + `warp.py` (3D inherent-strain warping & residual stress) + `checks.py` + `export3mf.py` (with per-object settings and height-range modifiers), **plus** optional user-uploaded `.gcode` / `.gcode.3mf` (which already activates `gcode.spot_heat` and G-code-timed warping) or local mode `fdmdfm` (which runs real OrcaSlicer CLI).
     - **Plugs in cleanly later:** `T-473`–`T-481` thermal optimizer and `T-127` browser WASM slicer drop into the exact same Step 9 / Step 11 slots without any UI rewrite.

2. **Wrong module reference & Pyodide `LOCAL_ONLY` violation in `Complete` depth:**
   - Claude writes: *"Complete only: weak-spot regions from the stress field → try `additions.py`/`placement.py` edits (hidden gaps for solid walls, plastic added, modifiers with more walls/infill), each re-solved."*
   - **Three errors here:**
     1. `placement.py` handles **bed rotation and plate copy layout**, not geometry additions.
     2. Real mesh-boolean edits (`add_solid`, fillets, ribs) live in `booleans.py` (`manifold3d`, **`LOCAL_ONLY`** — forbidden in Pyodide!) or `web/geometry.js` (`manifold-3d` WASM in the browser main thread). A Python `api.optimize()` call running in Pyodide **cannot** run mesh booleans inside its optimization loop.
     3. Hidden gaps (`Zone(..., void=True)`) and stronger modifier zones (`Zone(...)`) **already work purely in voxel space** inside `strength.py` without any mesh boolean!
   - **Realistic Pyodide-safe design:** Inside Python `api.optimize()` at `Complete` depth, test: (a) local `Zone` modifiers (more walls/infill around high-stress voxels), (b) `void` hidden gaps (pin-holes forcing solid internal walls), and (c) voxel-space added-plastic candidates (e.g. thickening weak-spot surface voxels / adding inside-corner fillet voxels directly on the `VoxelGrid`, warm-started with `x0` in CG so each solve takes ~1–2 s). Only emit the actual geometry edit meshes (`additions.py` / `features.py`) for the winning 2–4 options, applied via JS `manifold-3d` only if the user picks that option!

3. **"Thicker layers are stronger" + 1.0 mm nozzle do not exist in the engine yet:**
   - Claude casually writes `(thicker layers are stronger per layer interface — model it [F])` and `0.8/1.0 for big parts`.
   - **Repo reality:**
     - `core/fdmcore/data/presets.yaml` only defines nozzles `"0.2"`, `"0.4"`, `"0.6"`, `"0.8"`. There is **no `"1.0"` nozzle preset**.
     - `Profile.layer_height` in `core/fdmcore/profile.py` is a read-only property locked to the nozzle preset; `Profile` does not accept a custom `layer_height` override yet.
     - `strength.py`'s `material_for(profile)` computes `strength_z = tensile_xy * z_strength_factor * heat_factor` with **zero dependence on `layer_height`**.
     - Physics caveat (per `AGENTS.md` sourcing rule): interlayer bond strength does **not** grow monotonically with layer height — published tests (CNC Kitchen, Coogan & Kazmer) show bond strength increases up to ~50% of nozzle width (wider melt contact + retained heat) and **drops** when layer height exceeds ~65–75% of nozzle width (rounded bead cross-section, narrower weld neck). We must add a sourced, bounded `layer_height_bond_factor(lh, nozzle)` curve (capped around 0.5–0.6× nozzle) rather than naive "thicker = always stronger".

4. **Combinatorial blow-up in `optimize()` unless factored properly:**
   - Naively evaluating `orientations × materials × nozzles × layer_heights × (walls, top, infill)` in Pyodide would take hours.
   - **Fast factored math already supported by `fdmcore`:**
     - **Orientations:** 1 isotropic FEA solve (`strength.orientation_safety`) scores all candidate orientations in ~3–5 s.
     - **Supports:** Sliced `refine.support_stats` depends only on `(orientation, max_overhang_deg, nozzle)` — across all 8 built-in materials there are only **3 unique overhang angles** (`40°`, `45°`, `55°`). Cache `support_stats` per `(orientation, max_overhang_deg)` on only the top ~8–10 orientation finalists, never all 256 sphere directions.
     - **Materials:** Because linear-elastic stress is invariant to uniform stiffness scaling (`_scaled_safety` in `api.py:2177`), evaluating all environment-compatible materials for a given `(orientation, nozzle, shells)` is **O(1) arithmetic** (0 extra FEA solves!).
     - **Shells / Zones:** Warm-start CG (`x0=u_prev` in `fea.solve`) during greedy climbs cuts each step from ~3 s to ~0.6–1.2 s.

5. **`checks.py` already emits one finding per hole — the real culprit is text length and UI grouping:**
   - Claude's `T-551` assumes the engine merges multiple holes into one finding. In fact, `checks.py` (`_hole_findings` and horizontal holes) **already emits one finding dict per hole**. The wall-of-text problem came from multi-sentence `detail` strings (`"Sideways Ø6.0 mm hole at 12 mm: its top sags... a Ø6 pin won't slide in. For an M6 bolt..."`) and the old UI stacking full descriptions/explanations inline.
   - Fix: add concise `short_title` (≤ 6 words) and `one_liner` (≤ 80 chars) + `instance_label` (`"Ø6 mm side hole"`) on findings, keep full `detail` behind `"More"`, and link each `design` edit directly to its finding by `finding_id` / `bbox` overlap.

6. **Edge / inside-corner picking contract:**
   - Claude does not explain how an edge or inside-corner hold/load maps to `strength.py`. Fortunately, `strength.py` **already accepts polyline point clouds** (`{"points": [[x,y,z], ...], "radius": r, "kind": "fixed" | "force"}`). So `regions.py` + `features.concave_edges` can export clean feature-edge polylines once on load, Three.js highlights them on hover, and clicking an edge simply submits its sampled points to `strength.py` with zero FEA rewrite.

7. **MCP parity breakage in Claude's Phase E:**
   - `core/tests/` has `test_every_api_method_has_a_tool` (`T-261`), which **fails pytest** if `api.optimize` is added to `api.METHODS` without registering its MCP tool in `mcp_server.py`. Claude pushed MCP docs/tools to the very last task `T-572`. Registering the MCP tool must happen in the same PR as `api.optimize`.

---

### 1.3 What I would order differently

1. **Run Engine Foundations in parallel with UI Mocks (Phase 1 & Phase 2 concurrently):**
   - Claude puts all engine work (`optimize`, time model, Complete additions, short finding titles, material environment fields) in Phases C, E, and F — *after* mocks and shell. Because `core/fdmcore/` has zero UI dependency, engine tasks can run immediately while the owner reviews static HTML UI mocks. By the time the UI shell is ready, real engine APIs are already tested and waiting.
2. **Do not waste 3 PRs refactoring old CSS/JS in Phase A before throwing it away:**
   - Collapse Claude's Phase A into **one tiny 1-PR triage** on `main` (finish `T-491` header grid row + delete mobile `@media` rules + hide the Expert toggle) so the current branch stops breaking desktop immediately, without spending days refactoring `web/steps.js` or old CSS that Phase 3 replaces cleanly.
3. **Move Settings & Calibration Hub up to Phase 3 (with Setup), not Phase G:**
   - Step 2 (Setup) has an explicit button to manage printers, materials, and imported profiles. Building Settings in Phase G (Claude `T-561`) leaves Step 2 either broken or opening the old ugly modal for the entire rebuild. Build the clean Settings Hub shell right alongside Setup in Phase 3, and drop extra calibration test pages in Phase 7.
4. **Move `app.js` modularization into the shell design from Step 0:**
   - Don't "split `app.js` as steps are rebuilt" (Claude `T-525` tacked onto Phase C). Start the new UI in clean ES modules from the very first screen (`web/ui/`, `web/steps/`, `web/viewport/`, `web/state.js`) behind a clean entry cutover (`index.html`), deleting old legacy DOM/CSS in one tidy final cleanup phase once e2e passes.

---

### 1.4 Claude's open questions that can be answered without asking the owner

| Claude Q# | Question | Direct Answer (no need to ask) | Why |
|---|---|---|---|
| **Q1** | Settings & calibration: header gear only, or also on Start? | **Both** — third secondary card on Start + header gear icon. | Explicitly requested in `feedback.md` penultimate paragraph (*"in the first view where you have optimize print, fix print problems, and then we have like settings. Settings and calibrations"*). |
| **Q2** | Fast goal safety floor: 2.0 or 1.5? | **2.0** (bumped to 3.0 if continuous load / creep is on). | Sits naturally between Balanced (3.0) and Light (1.5), and matches the existing codebase baseline (`service.py`). Mention in notes; no blocker. |
| **Q3** | Quality goal from day one, or later? | **Include the 5th card in mocks & Setup from day one**; ship working Quality path (steps 1→2→3→7→8→9→10→11 skipping strength) in Phase 7, then enrich with quality-settings research. | Requested directly in `feedback.md`; architecture handles goal-based step skipping cleanly from the start. |
| **Q4** | Force units: N and MPa only, or also kg? | **N and MPa unit switch** (exact match to feedback), plus a quiet read-only `≈ X kg` conversion readout underneath for intuition. | Feedback explicitly says: *"Newton, in megapascal. Yeah, I think that's— the two units are fine."* |
| **Q5** | Hold types: just "held", or also resting / screw? | **1-click "Hold" in the 3D radial menu** with automatic smart defaults (round hole → `screw`, flat face → `clamped`), plus a compact `Fixed / Resting / Screw` toggle inside the hold's list row in the right panel. | `strength.py` already implements all three (`T-139`). Zero clutter in 3D, full FEA accuracy kept. |
| **Q7** | Should user list owned materials in Settings? | **Yes** — simple checklist in Settings → My materials. Options prioritize owned materials and badge unowned ones (`"Not in your shelf"`). When none are ticked, all suitable materials show with PLA always offered when feasible. | Directly solves the *"I only have PLA"* scenario in `feedback.md`. |
| **Q9** | Always 4 option columns, or 2–4? | **Up to 4 distinct columns (typically 2–4).** Collapse mirror orientations and near-duplicates (within 5% on grams, time, and safety). Never pad with fake duplicate columns. | Matches feedback (*"let's say makes four options"*, *"we can give him both options"*). |
| **Q10** | Approve static screen mocks before wiring logic? | **Yes — mandatory Phase 1 gate.** | Given the owner's frustration with the old UI, reviewing static screenshots before coding logic saves everyone wasted work. |

*(Only Claude's Q6 [big nozzle threshold — proposed concrete geometry rule below], Q8 [Tolerances 3D order], and Q11 [home for existing power features] remain worth confirming — alongside 3 critical new questions we uncovered in Section 6.)*

---

## 2. Architecture & Desktop Visual System

### 2.1 Non-negotiable global rules

1. **Desktop only (`≥ 1280 × 720`):**
   - Zero `@media (max-width: ...)` rules anywhere in `web/style.css`. No mobile drawers, no bottom sheets, no collapsed 3D strips, no touch-fallback hacks. Below `1280 × 720`, the window scrolls cleanly rather than mutating layout.
   - Update `PRODUCT.md` Operating Context to state desktop-only clearly.
2. **Header strictly pinned at the top:**
   - Outer app root is a full-viewport CSS Grid:
     ```css
     #app {
       display: grid;
       grid-template-rows: 52px minmax(0, 1fr);
       height: 100vh;
       overflow: hidden;
     }
     ```
   - The header is explicitly placed in `grid-row: 1` (`position: relative; z-index: 20`). Toasts, modals, and extension-injected DOM nodes sit in a separate `#portal-root` outside the grid flow so nothing can ever push the header down.
3. **Single flow now; forward-compatible for levels later:**
   - Remove the Simple / Basic / Advanced / Expert switch and delete the Expert scroll page completely.
   - Define the workflow in a single declarative step manifest (`web/flow.js`) where each step carries `{ id, label, optional, skipWhen(state), minTier }`. When Basic / Advanced / Expert return later, **Expert uses the exact same step-by-step layout structure as Advanced** (showing additional expert controls inside each step, never a giant scroll page).
4. **Strict plain-language copy budget (anti-"wall of text"):**
   - Control label: **≤ 4 words**. Optional hint: **1 line, ≤ 80 characters**.
   - Never put explanatory paragraphs between buttons. Never put parenthetical notes in labels (e.g., `"STL, 3MF or STEP"` — no `"(or saved project)"`).
   - Finding row (collapsed): **title ≤ 6 words** + **location tag** + **1-line impact**. Everything longer lives inside a collapsed `"More"` disclosure.
   - Every visible string goes through `t("key", params)` in `web/messages/en.js` (per `AGENTS.md`).
5. **Pyodide & performance discipline:**
   - Engine runs in one Web Worker (`web/worker.js`) so the browser stays responsive.
   - `scipy` stays lazily loaded only when strength/optimization/simulation runs.
   - Every long task (> 1 s) shows what stage is running, elapsed time, estimated remaining time, and a working **Cancel** button that preserves session state (`session_state` / `holes_state`).

---

### 2.2 Desktop Visual System (built from scratch)

Design direction: **Clean Precision Workshop** — inspired by modern desktop CAD and engineering tools (calm neutral surfaces, crisp 1 px borders, generous breathing room, zero visual noise, so the 3D part and color overlays are the hero).

#### Color tokens (rebuilt, neutral-first)
- **Light workbench (default)** — neutral cool-gray surfaces instead of the old green-tinted chrome; dark theme tokens ready in the same file if the owner prefers dark CAD mode (see Open Question 5):
  - `--bg-app`: `#F5F6F8` (outer canvas / rail)
  - `--bg-surface`: `#FFFFFF` (cards, inspector panel, popovers)
  - `--bg-subtle`: `#ECEFF3` (inputs, segmented tracks, secondary surfaces)
  - `--bg-stage`: `#E4E8EE` radial-gradient to `#D8DEE6` (3D viewport backdrop — neutral so green/blue/red/purple 3D overlays never clash)
  - `--border-subtle`: `#E1E5EC` (1 px dividers)
  - `--border-strong`: `#C5CDD8` (interactive control outlines)
  - `--text-primary`: `#111827`
  - `--text-secondary`: `#4B5563`
  - `--text-muted`: `#6B7280`
  - `--accent`: `#2563EB` (crisp engineering blue — primary buttons, active step, focus rings; hover `#1D4ED8`, soft `#EFF6FF`)
  - **Semantic status (used ONLY for status, never decoration):**
    - Good / Safe: `#15803D` (soft `#DCFCE7`)
    - Warning / Marginal: `#B45309` (soft `#FEF3C7`)
    - Danger / Fail: `#B91C1C` (soft `#FEE2E2`)
  - **3D overlay palette (high contrast against neutral gray part `#9CA3AF`):**
    - Hover highlight: `#38BDF8` (cyan fill 30% + 2 px outline)
    - Hold (fixed/screw/resting): `#2563EB` (cobalt blue + lock badge)
    - Load / Force arrow: `#9333EA` (vivid violet)
    - Design Add ghost: `#16A34A` (translucent emerald)
    - Design Cut ghost: `#DC2626` (translucent crimson)
    - Bed contact: `#059669` · Overhang warn: `#F59E0B` · Needs support: `#EF4444`

#### Strict Spacing Scale (enforced by automated CSS lint test)
Only these custom properties may be used for `margin`, `padding`, and `gap` outside `tokens.css`:
- `--s-1`: `4px` (icon-to-label gap, tight inline spacing)
- `--s-2`: `8px` (padding inside compact controls, chips)
- `--s-3`: `12px` (gap between related controls in a group, card inner padding small)
- `--s-4`: `16px` (standard card padding, panel horizontal gutter, gap between cards)
- `--s-6`: `24px` (gap between form groups inside a panel)
- `--s-8`: `32px` (major section spacing, Start screen gutters)
- `--s-12`: `48px` (hero empty-state padding)

A pytest lint test (`core/tests/test_css_tokens.py`) parses `web/style.css` and fails if any `padding`, `margin`, or `gap` declaration uses a raw `px`/`rem` number instead of `var(--s-*)` or `0`.

#### Typography Scale (Atkinson Hyperlegible, self-hosted)
- `--t-xs`: `12px` / `1.35` (badges, table sub-labels, 3D HUD captions)
- `--t-sm`: `13px` / `1.4` (hints, secondary metadata, rail items)
- `--t-base`: `14px` / `1.45` (body copy, controls, labels — crisp desktop density)
- `--t-md`: `16px` / `1.35` (panel titles, card headings)
- `--t-lg`: `20px` / `1.25` (screen titles, Settings tab headers)
- `--t-xl`: `28px` / `1.15` (Start screen hero title)
- Only two weights: `400` (regular) and `600` (semibold/bold). Numeric readouts use `font-variant-numeric: tabular-nums`.

#### Layout Geometries (exact pixel budgets)
1. **Top Header (`52px` fixed height):**
   - Left: Brand mark + `"FDM Check"` (click returns to Start with unsaved guard) · Divider · Active workflow title (`"Optimize your print"`).
   - Center: Compact part pill once loaded (`"bracket.step · 60 × 30 × 40 mm"`, click to swap part or view mesh health).
   - Right: `Undo` / `Redo` icon buttons (with tooltips `Ctrl+Z` / `Ctrl+Y`) · **`Export 3MF`** button (enabled from Step 2 once a part is loaded, splits into quick download + slicer selector popover) · **`Settings & calibration`** gear button · `Help` `?` button (keyboard shortcuts + where it runs).
2. **Standard Workflow Shell (Steps 1–7, 9–11):**
   - 3-column CSS Grid: `216px (Left Step Rail) | minmax(480px, 1fr) (3D Stage) | 384px (Right Inspector)`.
   - **Left Step Rail (`216px`):** Ordered vertical list of active steps. Each item (`36px` height) shows step state icon (completed check, active blue pill, upcoming muted number, skipped hidden for Quality). Clicking any completed step jumps back cleanly without losing later inputs unless you change an upstream parameter (with clear `"Re-run needed"` marker on downstream steps).
   - **Center 3D Stage (`1fr`):** Full-bleed WebGL canvas with minimal floating HUD:
     - Bottom-left: compact camera toolbar (`Iso`, `Top`, `Front`, `Fit`, `Section cut`, `X-ray`) — `32px` translucent pill bar.
     - Top-center (only when relevant): contextual instruction pill (e.g., Step 4: `"Hover a face, edge, or inside corner and click to hold it"`).
     - Top-right (only when an overlay is active): legend bar (stress / heat / warp / overhang).
     - Bottom-center: Toast slot (`max-width: 460px`, auto-dismiss or action button).
   - **Right Inspector (`384px`):** Strict 3-row internal grid:
     - Sticky Header (`auto`, `padding: 16px 20px`, `1px` bottom border): Step title (`16px` bold) + 1-line purpose (`13px` muted).
     - Scrolling Body (`1fr`, `overflow-y: auto`, `padding: 20px`, `gap: 24px`).
     - Sticky Footer (`64px`, `padding: 12px 20px`, `1px` top border): Left = **Back** (ghost), Right = **Next / Run** (primary, always in the exact same screen coordinates across every step).
3. **Wide Split Shell (Step 8 — Options):**
   - Left Step Rail collapses smoothly to a `56px` icon-only rail (hover shows labels) to give maximum breathing room.
   - Grid becomes: `56px (Rail) | 38% (3D Stage, min 400px) | 62% (Options Comparison Deck, min 680px)`.
   - Gives 4 side-by-side option columns (`~165–210px` each) full readability at `1280px` and generous comfort at `1440px+`.
4. **Full-Canvas Shell (Start Screen & Settings Hub):**
   - No 3D stage or step rail; centered container (`max-width: 1040px` for Start; `220px left tab nav | 1fr content` for Settings Hub).

---

### 2.3 Component Library (`web/ui/*.js`)

Every control is a dedicated, styled ES module component — **zero raw browser `<select>`, `<input>`, or `<details>` appearance**. Every component supports `default`, `hover`, `focus-visible` (`2px solid var(--accent)` with `2px` offset), `active`, `disabled`, and `error` states:

- `Button.js`: `primary | secondary | ghost | danger`, sizes `sm (30px) | md (36px) | lg (42px)`, optional leading icon, loading spinner state.
- `ChoiceCard.js`: Selectable card (`12px` padding, `8px` radius, `1.5px` border, subtle tinted fill + checkmark when selected). Contains icon + title (≤ 4 words) + optional 1-line subtitle. Keyboard arrow-key navigation.
- `SegmentedControl.js`: 2–5 mutually exclusive options in a pill track (`36px` height) with sliding active thumb.
- `Chip.js`: Toggleable multi-select pill (`32px` height) with icon + label.
- `Select.js`: Custom searchable dropdown popover with keyboard navigation (`↑/↓/Enter/Esc`), optional top `"Decide later"` item, optional group headers, and inline badge (e.g., `"Owned"`).
- `NumberField.js`: Styled numeric input with inline unit suffix or inline segmented unit switch (`N | MPa`), step arrows, validation message slot, and optional quiet secondary conversion readout (`≈ 5.1 kg`).
- `Disclosure.js`: Clean `"More ▸"` button that expands smoothly into a muted inset card (`12px` padding) for explanations, numbers, and sourced links.
- `RadialMenu.js`: Screen-space ring/pill menu anchored to a projected 3D world coordinate (`Vector3.project(camera)`), repositioned on every orbit frame, clamped inside the 3D stage bounds, closed on `Esc` or outside click.
- `Popover3D.js`: Compact floating card (`240px` wide) anchored to a 3D point/arrow tip (used for Force input and Tolerance fit picker).
- `OptionColumn.js`: Comparison column card for Step 8 with Pros/Cons top block, aligned metric rows with hover-to-pin lock buttons, inline value editor popover, and bottom CTA.
- `FindingRow.js`: Compact problem row (`48px` collapsed) with severity dot, short title, instance badge, 1-line consequence; expands to show `More` + inline Fix rows (`Slicer / Design / Process` tag + `Apply / Pending / Undo`).
- `ProgressCard.js`: Shows active stage label, determinate/indeterminate bar, elapsed time, ETA, `Cancel` button, and optional `"Stop & use best so far"` button.
- `Toast.js`: Floating bottom-center notification (`info | warn | error | success`), optional `"More"` or `"Undo"` action.
- `Modal.js`: Accessible `<dialog>` wrapper for rare confirmations (e.g., `"This changes your 3D model"`, multi-body picker, Delete all recent projects).

---

### 2.4 Keyboard Shortcuts, State Persistence & Home for Existing Power Features

- **Keyboard shortcuts:**
  - `Esc`: Close active radial menu, 3D popover, or modal; deselect hovered/active item.
  - `Enter`: Confirm active 3D force/tolerance popover.
  - `Delete` / `Backspace`: Remove currently selected hold or load.
  - `Ctrl+Z` / `Ctrl+Y` (`Ctrl+Shift+Z`): Undo / Redo (preserves holds, loads, design versions, pending edits).
  - `1` / `2` / `3` / `F`: Front / Top / Isometric / Fit camera views when 3D stage is focused.
  - `X`: Toggle X-ray mode (to pick internal holes/inside corners easily).
- **Autosave & Project files:**
  - Keep existing `IndexedDB` binary Blob autosave (`T-460`) and 10-project recent history (`T-453`), surfaced cleanly on the Start screen.
  - Opening or saving a `.json` project file (shared with CLI/MCP `T-266`) lives in the Start screen (`"Open saved project"`) and inside the Export/File dropdown in the header + Done screen — keeping the Step 1 drop zone 100% clean.
- **Clean homes for existing built features (zero accidental deletion):**
  - *Split part (too big)* → Auto-surfaced as an actionable fix directly under the `exceeds_build_volume` finding in Step 10 AND under Step 11 Advanced Tools.
  - *Split part (less support)* → Surfaced as a fix under heavy-support findings in Step 10 AND in Step 11 Advanced Tools.
  - *Pause to insert (magnets/nuts)* → Surfaced directly under `pause_to_insert` findings in Step 10 (with side-slot design fix right beside it) AND toggleable in Step 11 Export.
  - *Numbers for CAD* → Built directly into Step 10 (every design fix shows its exact CAD number) + one-click `"Copy CAD checklist"` button on Step 11 Done.
  - *Shareable HTML report (with embedded project)* → Secondary button on Step 11 Done (`"Share report"`).
  - *Copies on the plate*, *Electronics box heat*, *Manual modifier zones / hidden gaps*, *A/B compare* → Organized cleanly inside the `"More tools"` drawer on Step 11 Done (each tagged `"Your slicer can do this too"` where applicable per `PRODUCT.md` Principle 6).

---

## 3. Detailed Screen-by-Screen Flow

```
Start ──┬── Optimize your print ──► 1 Part ──► 2 Setup ──► 3 Environment
        │                                                       │
        │        ┌─────────────── (goal = Quality) ─────────────┤
        │        │                                              ▼ (Balanced / Strong / Fast / Light)
        │        │                                       4 Holds (3D) ──► [optional: Skip strength*]
        │        │                                              │                    │
        │        │                                       5 Loads (3D)                │
        │        │                                              │                    │
        │        │                                       6 Load details              │
        │        │                                              │                    │
        │        └──────────────────────────────────────────────┴────────────────────┘
        │                                                       ▼
        │                                              7 Analyse (Fast / Extended / Complete)
        │                                                       ▼
        │                                              8 Options (2–4 columns + Constraints bar)
        │                                                       ▼
        │                                              9 Print simulation (Heat, Cooling, Warping)
        │                                                       ▼
        │                                             10 Problems & fixes (Pending → Confirm all)
        │                                                       ▼
        │                                             11 Done & Export + Optional Extras
        │                                                (Tolerances 3D · Lighter · Tuned G-code · More tools)
        │
        ├── Fix your broken prints  (Coming soon card)
        └── Settings & calibration  (Start card + always in Header gear)
```

---

### Screen 0 — Start Screen

- **Layout:** Centered container (`max-width: 1000px`), generous top whitespace (`48px`), no step rail, no 3D canvas, no Export button in header.
- **Hero heading:** `"What would you like to do?"` (`28px` bold, 1 line, zero subtitle clutter).
- **Row 1 — Two equal large workflow cards (`grid-template-columns: 1fr 1fr; gap: 24px; min-height: 220px`):**
  1. **Left Card — `"Optimize your print"`** (interactive primary card, subtle accent border on hover, whole card clickable):
     - Clean geometric icon + Title (`20px` bold) + 1 line: `"Find the best orientation, settings, and material for your part."`
     - Bottom CTA arrow: `"Start →"`.
  2. **Right Card — `"Fix your broken prints"`** (muted surface, non-clickable, clean `"Coming soon"` pill badge top-right):
     - Title (`20px` bold) + short scannable list (3 bullets):
       - `"Match warping, stringing, or layer splits to your part"`
       - `"See why a print failed and what to change"`
       - `"Photo-assisted troubleshooting later"`
- **Row 2 — Settings & Calibration card (`margin-top: 24px`, full-width horizontal card, `72px` height):**
  - Gear icon + `"Settings & calibration"` + 1 line: `"Your printers, materials, slicer profiles, and calibration test prints."` + `"Open →"`.
- **Row 3 — Recent Projects (`margin-top: 32px`, shown ONLY if user has saved projects in IndexedDB; hidden on first visit):**
  - Header: `"Recent projects"` + right-side `"Open project file…"` link + `"Clear all"` ghost button.
  - Up to 3 compact horizontal cards (thumbnail, filename, date, verdict pill, delete `×`). If > 3 exist, `"Show all (N)"` expands cleanly.
  - If no recent projects exist, just one quiet centered link at the bottom: `"Have a saved project? Open file"`.
- **Empty / First-visit state:** Engine boots silently in the background worker while the user looks at Start; subtle status dot in header footer only if still warming up (no blocking spinner on Start).

---

### Step 1 — Your Part

- **Layout:** Standard 3-column shell, **except** the Right Inspector is hidden until a part loads so the drop zone owns the full stage (`216px rail | 1fr drop stage`).
- **Center Stage:**
  - One crisp, centered drop card (`560px × 320px`, dashed `2px` border that turns solid accent blue on drag-over):
    - Upload icon (`40px`)
    - `"Drop your part here"` (`20px` bold)
    - Primary button: **`Choose file`**
    - Muted format line: `"STL, 3MF, or STEP"` *(nothing in brackets, no "or saved project")*
  - Directly below the drop card (`margin-top: 16px`): **exactly one button** — Secondary button **`Sample parts ▾`**.
    - Clicking opens a clean popover grid (`520px` wide, 2×4 grid of sample cards with rendered thumbnails and 2–3 word names like `"Shelf bracket"`, `"Wall hook"`, `"Enclosure"`, `"Cable clip"`). Clicking any sample immediately loads it.
  - **Zero extra filler sentences** below the drop card.
- **Loading state:** Drop card switches cleanly in-place to a `ProgressCard` (`"Reading bracket.step…"` with progress bar and Cancel).
- **Special load states (handled BEFORE auto-advancing):**
  1. **Multi-body file (`T-050`/`T-052`):** Shows the bodies in the 3D stage (distinct colors) and opens a compact inline picker card: `"This file has 3 separate parts. Click one to check."` Choosing a body advances to Step 2.
  2. **Unit sanity check (`T-051`/`T-306`):** If dimensions look like inches or cm/m (e.g. `2.4 × 1.2 × 1.5 mm`), advances to Step 2 and shows a non-blocking amber banner at the top of the 3D stage: `"This part is only 2.4 mm wide. Scale from inches (×25.4) or cm (×10)?"` with one-click buttons + Dismiss.
  3. **Damaged mesh (`T-459`):** Advances to Step 2 and shows a dismissible warning Toast with `"More"` explaining open edges/flipped faces and how to repair in the slicer.
  4. **Error state (corrupt / > 4M triangles / unsupported):** Stays on Step 1, displays error Toast with plain reason (`"This file has more than 4 million triangles"`).
- **Normal load:** Part renders centered in the 3D stage, camera smoothly frames it, and the flow **auto-advances to Step 2 (Setup)**.

---

### Step 2 — Setup ("What you have")

- **3D Stage:** Displays the loaded part cleanly in neutral workshop gray with subtle ambient occlusion and dimension ticks on hover.
- **Right Panel (`384px`):**
  - Header: `"What you have"` · `"Pick your printer and what matters most."`
  - **Group 1 — Hardware & Material:**
    - **Printer** (required): Searchable `Select` (built-in presets + user's custom printers + bottom item `"Add another printer (+415 more)…"`).
    - Right beside the Printer label: small ghost button **`Manage`** (gear icon) that opens the Settings & Calibration Hub directly to the Printers & Materials tab and returns right here when closed.
    - Two-column compact row (`gap: 12px`) directly below Printer:
      - **Material** `Select` — default: **`Decide later`** (meaning the optimizer selects the best material for environment & load). Also lists user's owned materials at top, then built-in materials.
      - **Nozzle** `Select` — default: **`Decide later`** (optimizer selects `0.2`–`1.0 mm` within printer & part limits).
  - **Group 2 — What matters most?**
    - 5 clean `ChoiceCard` tiles in a compact grid (or 5-item visual list with icon + name):
      1. **Balanced** — *"Good strength, weight, and print time."* (default)
      2. **Strong** — *"Maximum safety margin; extra plastic is fine."*
      3. **Fast** — *"Shortest print time that still holds."*
      4. **Light** — *"Least plastic including supports."*
      5. **Quality** — *"Best surface finish; skips strength checks."*
    - Only the **currently selected** card shows its 1-line description (keeps vertical space tight and scannable).
  - **Group 3 — What can you change?**
    - 3-way `SegmentedControl` (or 3 compact side-by-side `ChoiceCard`s):
      - **Slicer settings** *(default)*
      - **The design**
      - **Both**
- **Footer:** Back (to Step 1) · Primary **`Next`**.
- **Remembered defaults:** Printer and `"What can you change?"` persist in `localStorage`; Material and Nozzle default to `"Decide later"` on fresh runs (so users get the optimizer's help by default) unless they explicitly lock them.

---

### Step 3 — Environment

- **Right Panel (`384px`):**
  - Header: `"Where will it be used?"` · `"Helps pick the right plastic and print settings."`
  - **Group 1 — Temperature:**
    - Compact 2-column grid of `ChoiceCard`s (reuses existing temperature tiers with explicit °C ranges):
      - **Indoors** (`~20 °C`) *(default)*
      - **Warm room / electronics** (`~40 °C`)
      - **Outdoors in sun** (`~50 °C`)
      - **Hot car / near motor** (`~70 °C+`)
      - **Freezing cold** (`< 0 °C`)
      - **Custom…** (reveals single `NumberField` in °C only if clicked)
  - **Group 2 — What will it touch?** *(Multi-select icon `Chip`s, wraps cleanly in 2 columns)*:
    - `Water / wet` (favors PETG/ASA/PC; warns on hygroscopic PA-CF absorption)
    - `Food or drinks` (triggers food-safety note: FDM layer gaps trap bacteria; recommends PETG/PLA + food-safe coating/liner)
    - `Outdoors & sun (UV)` (favors ASA/PETG; penalizes PLA/ABS)
    - `Dirt & scraping` (favors PETG/PA-CF/PC/TPU; abrasion resistance)
    - `Oil, grease & fuel` (workshop / machine shop — favors PA-CF/PETG; warns on ABS/PC chemical stress cracking with certain oils/solvents)
    - `Cleaners & alcohol` (favors PETG/PA; warns on ABS + acetone / PC + strong cleaners)
    - `Skin / worn` (skin-friendly warning against carbon-fiber splinters in PA-CF!)
    - `None of these` *(default active; toggling any other chip deselects it)*
  - **Live feedback pill (1 line at bottom of body):**
    - If Material = `"Decide later"`: `"Compatible materials: PLA, PETG, ASA (+3 more)"` (updates live as chips toggle; if 0 compatible materials for the chosen printer — e.g., Hot car + Open printer — shows an amber warning pill: `"Needs high-temp plastic; your open printer may need an enclosure."`).
    - If user fixed a Material in Step 2 that fails the environment (e.g., PLA + Hot car): immediate inline warning card with 1-click **`Switch material to "Decide later"`**.
- **Engine data requirement:** Extend `core/fdmcore/data/presets.yaml` materials with sourced fields: `water_resistance`, `food_notes`, `abrasion_resistance`, `oil_fuel_resistance`, `solvent_resistance`, `skin_safe` (already has `heat_limit_c`, `uv_resistance`, `hygroscopic`, `fibre`).
- **Footer:** Back · Primary **`Next`**.
  - If Goal = **Quality**: jumps straight to **Step 7 (Analyse)** (Steps 4–6 hidden from rail).
  - Otherwise: advances to **Step 4 (Holds)**.

---

### Step 4 — Holds ("Where is it held?") — 3D Spatial Interaction

**Everything spatial happens on the 3D model, right at the cursor.**

- **Precomputation (on entering Step 4, fast background call):**
  - Engine returns `regions` (face region ID per triangle, area, normal, kind: `flat | cylinder | hole | curved`), `feature_edges` (polyline segments where dihedral angle > 25° or region boundary), and `inside_corners` (from existing `features.concave_edges`).
- **3D Hover Behavior:**
  - Mouse move triggers `requestAnimationFrame`-throttled raycast against the part mesh.
  - **Smart pick priority:**
    1. If cursor is within `6px` screen distance of an **inside corner** or **feature edge** segment → highlight that continuous edge polyline in cyan (`3px` glowing tube/line) + small cursor badge (`"Inside corner"` or `"Edge"`).
    2. Otherwise → highlight the **face region** under the cursor (soft cyan translucent tint + crisp boundary outline; for cylindrical holes, highlights the full bore and badges `"Ø5 mm hole"`).
  - Nothing is selected on hover; user always sees *exactly* what clicking will grab.
- **3D Click → Radial Menu at Cursor:**
  - Clicking anchors a crisp `RadialMenu` at the clicked 3D point (follows camera orbit; `Esc` or click-away cancels):
    - When hovering a face: **`Hold this face`** (primary) · **`Paint area`** (optional brush toggle for holding part of a big face, using existing `points` API) · **`Cancel`**.
    - When hovering a hole: **`Hold screw hole`** (auto-uses `hold: "screw"`!) · **`Hold whole face`** · **`Cancel`**.
    - When hovering an edge / inside corner: **`Hold this edge`** (or **`Hold inside corner`**) · **`Hold face instead`** · **`Cancel`**.
  - Clicking confirm immediately renders the hold on the part:
    - Cobalt-blue highlight (`#2563EB`) + floating lock glyph (always visible via subtle depth-offset/X-ray so it's never hidden behind the part, keeping `T-132`).
  - Clicking an **existing** hold opens a tiny 2-item radial menu: **`Change hold type`** (`Fixed / Resting / Screw`) · **`Remove hold`**.
- **Over-constraint warning:**
  - Multiple holds allowed freely. When the user adds a 4th hold **or** total held area exceeds `30%` of the part's surface area, show a gentle bottom-center Toast:
    - `"Hold only where the part is truly fixed — too many holds can hide weak spots."`
- **Right Panel (`384px`):**
  - Header: `"Where is it held?"` · `"Click faces, holes, edges, or inside corners on the model."`
  - **Empty state (0 holds):** Clean 2-step illustrated micro-guide (`"1. Hover over the part to preview"` · `"2. Click to lock where it's mounted or screwed"`).
  - **Hold list:** One compact row per hold:
    - Lock icon + Auto-label (`"Ø5.0 mm screw hole"`, `"Bottom face · 412 mm²"`, `"Inside corner · 28 mm"`).
    - Compact inline 3-way pill (`Fixed | Resting | Screw`, preselected automatically).
    - Hovering a row pulses it in 3D; clicking focuses camera; `×` removes it.
  - **Escape hatch (for Balanced / Fast / Light only):** Quiet link above footer: `"Part carries no load? Skip strength check →"` (jumps to Step 7 with strength disabled).
- **Explicitly removed:** No `"Keep clean"`, no `"Put on the bed"`, no bed-orientation choices here.
- **Footer:** Back · Primary **`Next`** (enabled once `≥ 1` hold exists; helper text in footer when enabled: `"Or click more spots to hold"`).

---

### Step 5 — Loads ("Where does it get pushed or pulled?") — 3D Spatial Interaction

**Zero angle text boxes. Zero side-panel creation forms.**

- **3D Hover → Click → Direction Gizmo → Force Popover sequence:**
  1. **Hover:** Same face / hole / edge / inside-corner highlight as Step 4.
  2. **Click:** Opens the **3D Direction Gizmo** anchored right at the clicked point (surface centroid or clicked edge midpoint):
     - **Quick-pick arrows (1-click direction):**
       - Face normal pair: **Push in** and **Pull out** (colored bright violet, highlighted on hover).
       - World axes: **±X, ±Y, ±Z** (subtle RGB axis ticks so "straight down / gravity" or "sideways" is 1 click).
     - **3D Rotation Rings (slicer-style rotate gizmo):**
       - Three clean circular drag rings (X, Y, Z) around the anchor point to tilt the arrow to *any* custom angle in 3D space.
       - Snaps in `15°` increments by default; hold `Shift` for smooth `1°` free rotation.
       - A tiny read-only angle pill follows the drag (`"45°"`) — **never an editable angle input box**.
       - Clicking a quick-pick arrow commits the direction immediately and opens the Force Popover; dragging a ring updates the arrow live and shows a floating **`Set force →`** button (or opens the popover cleanly on drag-release).
  3. **Force Input Popover (anchored right beside the arrow tip):**
     - `NumberField` auto-focused on open (ready to type immediately!).
     - Inline segmented unit switch: **`N | MPa`**.
       - In **`N`** mode: typing `50` shows a quiet live hint below: `"≈ 5.1 kg of weight"`.
       - In **`MPa`** mode (pressure distributed over selected face area): typing `3` computes and displays live: `"= 1,236 N across 412 mm²"`. (If the user picked an edge or point with zero face area, `MPa` is disabled with tooltip `"Pick a face to enter pressure in MPa"`.)
     - Pressing **`Enter`** or clicking **`OK`** commits the load! `Esc` cancels.
  4. **Committed 3D visual:**
     - Clean violet 3D arrow (length log-scaled with force magnitude) with a compact pill tag (`"50 N"` or `"3 MPa"`).
     - Clicking any existing arrow focuses it and reopens its gizmo + force popover for instant editing.
- **Right Panel (`384px`):**
  - Header: `"Where are the loads?"` · `"Click where it gets pushed, pulled, or holds weight."`
  - **Empty state:** Micro-guide (`"1. Click a face or edge"` · `"2. Pick or drag arrow direction"` · `"3. Enter force in N or MPa"`).
  - **Load list:** One compact row per load:
    - Direction glyph + Location (`"Top hook · Pull"`), Value (`"50 N"`), Edit icon (reopens 3D gizmo/popover), Remove `×`.
  - **Multiple Load Cases (existing `T-030`, tucked cleanly):**
    - Default: all loads act together (`"Load case 1"`).
    - A quiet **`+ Add separate load case`** button at the bottom of the list lets advanced users test mutually exclusive load situations (e.g. `"Case 1: Hanging"` vs `"Case 2: Pulled sideways"` — worst case wins). Zero clutter when not needed.
- **Footer:** Back · Primary **`Next`** (enabled once `≥ 1` load exists).

---

### Step 6 — Load Details

- **Right Panel (`384px`):** Short, fast confirmation screen (fits easily without scrolling).
  - Header: `"Load details"` · `"Three quick checks before we optimize."`
  - **1. How long does the load act?** (3 `ChoiceCard`s):
    - **Short moments** *(default)* — *"Occasional use, lifting, pressing."*
    - **Often, on and off** — *"Repeated cycles."*
    - **All the time** — *"Hangs permanently (plastic slowly creeps)."* (raises effective safety floor per `service.py` / `CONSTANT_LOAD_SAFETY`)
  - **2. Temperature during load** *(Prefilled card from Step 3)*:
    - Compact info card: `"Checking strength at 50 °C (outdoors in sun). Is that right?"`
    - Two inline buttons: **`Yes`** *(selected by default)* · **`Change`** (reveals a single °C `NumberField`).
  - **3. Shaking or knocks?**
    - Clean **`No | Yes`** `SegmentedControl` *(default `No`)*.
    - Selecting **`Yes`** reveals two compact options:
      - `Light vibration` (optional mounted weight in `g` / `kg` → triggers `api.dynamics` frequency check)
      - `Hits or drops` (drop height in `cm` / `mm` → triggers `impact_factor` multiplier)
  - **Zero print-strength / layer-adhesion inputs** (removed per feedback).
- **Footer:** Back · Primary **`Next`**.

---

### Step 7 — Analyse ("How deep should we search?")

- **Right Panel (`384px`):**
  - Header: `"Ready to optimize"` · `"Choose how thoroughly we search orientations, settings, and materials."`
  - **Three Depth `ChoiceCard`s** — each displays a **part-specific time estimate** computed from triangle count, bounding-box volume, and voxel count (calibrated against the corpus, e.g., `"~45 sec"` vs `"~4 min"` vs `"~14 min"`):
    1. **Fast** — *"Best print orientation + quick setting sweep."* (e.g. `~30 s – 2 min`)
    2. **Extended** *(default)* — *"Tests all orientations, walls, infill, layer heights, nozzle sizes, and suitable materials."* (e.g. `~2 – 8 min`)
    3. **Complete** — *"Everything in Extended, plus automatically tests local reinforcement zones, hidden wall-forcing gaps, and weak-spot fillets."* (e.g. `~10 – 25 min`)
       - If user picked `"Slicer settings only"` in Step 2: **Complete** is still visible (so users know it exists) but disabled with badge `"Requires design changes"` and a 1-click `"Allow design changes"` link.
       - If Goal = **Quality**: **Complete** is hidden (design additions aren't wanted for cosmetic/quality parts).
  - **Primary CTA:** Big **`Start optimization`** button (`42px` height).
- **Running state:**
  - The depth cards fold cleanly into a `ProgressCard` showing:
    - Active stage checklist with checkmarks & live active spinner:
      1. `Finding orientations & bed stability`
      2. `Checking strength across orientations` *(skipped for Quality)*
      3. `Measuring support grams`
      4. `Searching nozzles, layer heights & settings`
      5. `Testing local reinforcements` *(Complete only)*
      6. `Picking best diverse options`
    - Elapsed time + dynamic ETA.
    - **Owner-friendly upgrade:** Once Stage 4 produces at least 1 valid candidate, a secondary button **`Stop & show best so far`** appears beside **`Cancel`**, so a user waiting on a 15-minute Complete run can inspect good results immediately without throwing away 10 minutes of work!
  - In the 3D stage: the part gently previews candidate orientations as they are scored (low-FPS subtle transition, respects `prefers-reduced-motion`).

#### 7.1 Optimizer Engine Specification (`api.optimize()`)

New Pyodide-safe engine module `core/fdmcore/optimizer.py` exposed via `api.optimize(...)` and registered in `mcp_server.py`.

1. **Goal Safety Floors (hard lower bounds):**
   | Goal | Base Floor | With Creep ("All the time") | Notes |
   |---|---|---|---|
   | **Strong** | `5.0` | `6.0` | Maximizes safety; support grams have near-zero weight. |
   | **Balanced** | `3.0` | `3.5` | Balances print time, total grams (part + support), and reserve above floor. |
   | **Fast** | `2.0` | `3.0` | Minimizes print time; aggressively tests thicker layers & larger nozzles. |
   | **Light** | `1.5` | `2.5` | Minimizes **total grams (part + support)**; support weight has highest penalty. |
   | **Quality** | *(no FEA)* | *(no FEA)* | Minimizes support scars/volume + surface defects + warping; default 2 walls, 10% infill. |
   - Candidates below the goal's floor are rejected from normal ranking. If **no** candidate meets the floor, the optimizer does **not** return an empty list — it returns an explicit `feasible: false` result with the highest-safety candidates found and concrete recovery suggestions (`"Switch to PETG/PA-CF"`, `"Allow design changes"`, or `"Accept best reachable ×2.1"`).

2. **Orientation & Support Grams Pipeline (fast, Pyodide-safe):**
   - Generate candidate directions via `orientation.candidate_directions` + `resting_directions` (convex hull poses for organic parts). Collapse mirror-symmetric orientations (`|up_a · up_b|` with equivalent support/stress profiles).
   - Run **one** isotropic FEA solve (`strength.orientation_safety`) to score strength across all candidates in ~3–5 s.
   - Select top ~6–10 Pareto finalists (covering strongest, least-support proxy, lowest height, largest bed contact).
   - Run sliced `refine.support_stats` on finalists, cached per unique `max_overhang_deg` (`40°`, `45°`, `55°`) to compute realistic `support_g`, `scar_area`, and `bed_area`.

3. **Nozzle, Layer Height & Material Search Space:**
   - **Nozzles:** Add `"1.0"` nozzle entry to `presets.yaml` (`line_width: 1.05`, `layer_height: 0.5`, sourced speed factor). Filter candidate nozzles by:
     - Supported by the selected printer (or standard `0.4 / 0.6` if unknown).
     - `0.2 mm` only considered when fine detail/small holes exist or Goal = Quality.
     - `0.6 mm` always allowed unless thinnest wall `< 1.2 mm` or tiny holes close up.
     - `0.8 mm` / `1.0 mm` **only** considered on large parts (e.g., longest dimension `≥ 120 mm` OR solid volume `≥ 100 cm³` AND thinnest feature `≥ 2 × line_width`), matching feedback (*"if there are small parts we will not really suggest bigger nozzles than 0.6, but if you have like huge parts, we could suggest also, especially if we want to print fast, suggest bigger nozzles"*).
   - **Layer Height:** Allow `Profile` to take an explicit `layer_height` override (e.g. `0.35×`, `0.50×`, `0.65×` nozzle diameter, clamped to `0.08–0.75 × nozzle`). Model interlayer bond vs. layer height with a sourced, bounded factor (`layer_bond_factor`, peaking near `0.5–0.6 × nozzle`, dropping above `0.7 × nozzle` so we never recommend unprintable tall layers).
   - **Materials:** Filter all built-in + user custom materials by Step 3 Environment (`use_temp_c < heat_limit_c`, UV/water/oil/skin rules, printer enclosure & hardened nozzle compatibility). Because stress is invariant to uniform stiffness scaling, evaluate every compatible material analytically in O(1) (`_scaled_safety`) before verifying the winning finalists with an anisotropic solve.
   - **Shells & Infill:** Greedy climb (`walls` 2–8, `top/bottom` 3–8, `infill` 10%–75%, pattern `gyroid / grid / lightning` — disallowing `lightning` whenever loads exist) using warm-started CG (`x0`) and goal-weighted cost (`time`, `part_g + w_support * support_g`).

4. **Complete Depth — Pyodide-Safe Local Reinforcements:**
   - Identify top high-stress voxel clusters (`fi > 0.7 × max_fi`) on the top 2 orientations.
   - Test three Pyodide-safe strategies on the voxel grid (zero `booleans.py` / `manifold3d` calls inside Python):
     1. **Modifier Zones (`strength.Zone`):** Light base (2 walls, 10–15% infill) + dense modifier box/cylinder around weak spots (4–6 walls, 40–60% infill) → exported natively as 3MF modifier volumes.
     2. **Hidden Gaps (`Zone(..., void=True)`):** Internal 0.6 mm pin-holes placed along high-stress ribs/walls (`api.volume_at(..., kind="void")`) forcing the slicer to generate solid internal wall tubes.
     3. **Local Weak-Spot Geometry (`features._stress_chamfers` / `_thicker_weak_spot`):** Score inside-corner 45° fillets or local thickening directly on the voxel grid; if a candidate beats uniform settings on weight or reaches an otherwise unreachable safety floor, package its edit mesh with the option so the browser applies it cleanly via `manifold-3d` when chosen.

5. **Diverse Option Selection (2–4 Columns):**
   - From the Pareto frontier, pick up to 4 genuinely distinct, non-dominated options with human names:
     - Column 1: **Best for your goal** (e.g. `"Best balanced"`, `"Strongest"`, `"Fastest"`, `"Lightest"`).
     - Column 2: **Lowest support / easiest print** (if a cleaner orientation exists above the safety floor).
     - Column 3: **PLA / Owned material option** (if Column 1 chose PETG/ASA/PC and an owned filament or PLA also passes or gets close).
     - Column 4: **Fast / big-nozzle** OR **Reinforced (Complete depth)** alternative (when materially different by > 10% time, > 15% grams, or uses local zones/additions).

---

### Step 8 — Options (Comparison Columns + Constraints Bar)

- **Layout:** Wide Split Shell (`56px icon rail | 38% 3D Stage | 62% Comparison Deck`).
- **Left (3D Stage):**
  - Shows the currently highlighted/selected option's **print orientation on the virtual build plate**, overhang/support preview, and toggle pills at top-right: `Print view` · `Stress map` · `Design additions` (when Complete found zones/fillets).
  - Hovering any column's **Orientation** row immediately rotates the 3D preview smoothly to that option!
- **Right (Options Comparison Deck):**
  - Top bar: `"Choose an option"` + subtle count `"4 options meet your ×3 safety target"`.
  - **2–4 Side-by-Side `OptionColumn` Cards** (equal width, aligned horizontal rows so eyes scan straight across):
    - **Column Header:**
      - Option badge (`"Recommended"`, `"Least support"`, `"PLA only"`, `"Fastest"`).
      - **Pros** (up to 3 crisp green one-liners, e.g., `"Zero supports needed"`, `"Uses PLA you own"`).
      - **Cons** (up to 3 crisp amber one-liners, e.g., `"28 min slower"`, `"Needs 0.6 mm nozzle swap"`).
    - **Aligned Parameter Rows** (each row has a subtle hover Pin icon `📌` and click-to-edit value):
      1. `Orientation` (miniature bed icon + label like `"Flat on back"`)
      2. `Material` (e.g. `"PETG"`, with small pill `"Owned"` or `"Not in shelf"`)
      3. `Nozzle` (`"0.4 mm"` / `"0.6 mm"`)
      4. `Layer height` (`"0.20 mm"`)
      5. `Walls / Top-Bot` (`"3 walls · 4/3 layers"`)
      6. `Infill` (`"15% Gyroid"`)
      7. `Supports` (`"0 g"` or `"14 g · Tree"`)
      8. `Total plastic` (`"42 g"` — includes support; shows cost `"$0.84"` if price set)
      9. `Print time` (`"~1 h 18 m"`)
      10. `Safety` (traffic-light pill: `"Plenty · ~×5.4"` or `"Enough · ~×3.2"`, following `T-442` honest bands!)
      11. `Reinforcements` *(only if Complete depth)* (`"2 strong zones"` + `"Show"` toggle)
    - **Column Footer:** Primary/Secondary button **`Use this option`**.
- **Bottom Constraints Bar (sticky inside the deck — replaces 40 checkboxes):**
  - Empty state: quiet helper text `"Want to lock a material, nozzle, or orientation? Click 📌 on any value above, or "` + **`+ Pin a constraint`**.
  - Active state (after user pins/edits one or more values from any column):
    - Displays editable constraint chips: `[Material: PLA ▾ ×]` `[Nozzle: 0.4 mm ▾ ×]` `[Orientation: Flat on back ×]`
    - Right-aligned primary button: **`Re-optimize with my choices`** + ghost **`Clear`**.
  - Clicking **`Re-optimize with my choices`** re-runs `api.optimize(..., constraints={...})` quickly (skipping already-computed FEA/orientations where unchanged!) and replaces the columns with new options that strictly respect every pinned chip.
- **Infeasible Constraint State (honest & helpful):**
  - If constraints make the safety floor impossible (e.g., user pinned `PLA` on a heavy load):
    - Clean amber banner above the best-effort columns:
      `"No way to reach ×3.0 safety with PLA here — the strongest PLA setup reaches about ×2.1."`
    - Direct 1-click recovery buttons right in the banner: **`Allow PETG / ASA`** · **`Allow design changes`** · **`Accept ×2.1 anyway`**.

---

### Step 9 — Print Simulation

- **Purpose:** Verify thermal behavior, cooling, warping, residual stress, and printability for the exact chosen option before listing problems.
- **3D Stage:** Displays the part in its chosen print orientation on the build plate.
- **Right Panel (`384px`):**
  - Header: `"Print simulation"` · `"Check cooling, warping, and printability for your chosen setup."`
  - **Compact Chosen Setup Summary** (read-only strip: `"Flat · PETG · 0.4 mm · 3 walls · 15%"`, with `"Change"` back-link to Step 8).
  - **Print Environment Confirmation** (pre-filled from Printer & Material presets, collapsed into 1 compact editable grid — only 3 fields):
    - `Enclosure`: `Open / Passive / Heated`
    - `Bed temp`: `80 °C`
    - `Room temp`: `22 °C`
  - **Optional Detailed G-code Input (bridges today → future browser slicer):**
    - Subtle collapsible row: `"Have a sliced G-code file?"` — allows dropping a `.gcode` / `.gcode.3mf` (or auto-uses OrcaSlicer CLI in local mode `fdmdfm`) to unlock line-by-line thermal & G-code-timed warping right now. Once `T-127` (browser OrcaSlicer WASM) lands, this happens automatically with zero upload!
  - **Primary CTA:** **`Run print simulation`**.
- **What runs (honest Day-1 vs. Thermal Tier A roadmap):**
  - **Day 1 (works today in browser):**
    1. `checks.run_checks` (full printability & geometry checks)
    2. `heat.layer_heat` (1D layer cooling / softening check; upgraded to `gcode.spot_heat` if G-code supplied)
    3. `warp.simulate` (3D inherent-strain warping + residual internal stress)
  - **When `[TO]` Thermal Tier A (`T-473`–`T-476`) lands:**
    - Seamless drop-in inside the exact same button: adds full 2–4 mm cell heat network, too-hot/too-cold bonding window, delamination risk, and feeds local bonding back into the strength safety factor (`"Root safety: ×3.2 → ×2.8 after cooling"`).
- **Results state (in-place on Step 9 before advancing):**
  - Compact 3-metric status bar: `Cooling: OK / 1 hot spot` · `Warping: Low / Corners may lift` · `Print issues: 4 found`.
  - 3D Stage Overlay Switcher appears at top-right of canvas: **`Normal` | `Heat` | `Warping (curled)` | `Internal stress` | `Overhangs`**.
  - Sticky Footer switches primary CTA to **`Review problems (4) →`** (or **`All clear — finish →`** if 0 issues).

---

### Step 10 — Problems & Fixes

**Zero walls of text. One row per problem instance. Fixes live directly underneath their problem. Pending design changes preview live and batch-confirm.**

- **3D Stage:**
  - Clicking any `FindingRow` smoothly orbits the camera to frame that exact feature (e.g., that specific Ø6 mm hole), highlights its 3D bounding area, and dims unrelated overlays.
  - Applied **pending design changes** render immediately as translucent 3D ghosts (Green = added plastic, Red = cut plastic).
- **Right Panel (`384px`):**
  - Header: `"Problems & fixes"` · `"Click a problem to see where it is and how to fix it."`
  - **Top Banner (only after a failed/regressed Confirm):**
    - Amber banner: `"Your changes introduced 2 new problems (marked New below)."` with **`Undo last batch`** link.
  - **Grouped Finding Lists** (empty groups are hidden — never show empty headers):
    - `Can fail` (red dot)
    - `Shape off` (amber dot)
    - `Looks rough` (amber dot)
    - `Weak` (orange dot)
    - `Info` (slate dot — collapsed by default if there are higher-severity issues)
  - **Anatomy of a `FindingRow`:**
    - **Collapsed (default, `~52px` tall):**
      - Severity dot + **Short title (≤ 6 words)**: e.g., `"Side hole prints oval"`
      - Right-aligned location pill: `"Ø6.0 mm · Side"`
      - Second line (1 line max): `"Top sags ~0.3 mm; a 6 mm bolt won't fit."`
      - Status pill if fixed/pending/accepted: `"Fixed"` (green) / `"Pending"` (blue) / `"Accepted"` (gray).
    - **Expanded (on click):**
      - **`More ▸`** disclosure (collapsed by default): holds the full technical explanation, measured numbers, and clickable sources.
      - **`Fixes`** section directly underneath (compact stacked rows):
        - Each fix row has:
          - Domain pill: `Slicer` (teal) | `Design` (blue) | `Process` (gray)
          - Plain 1-line action + exact CAD number when applicable: e.g., `"Make teardrop hole (45°)"` or `"Turn on tree supports"` or `"CAD tip: thicken wall to 1.35 mm (now 1.0 mm)"`
          - Action button: **`Apply`** (for automated Slicer or Design fixes) or **`Mark noted`** (for Process/CAD-only tips).
          - Optional inline parameter tweak for adjustable design edits (e.g., teardrop angle `45°`, chamfer `1.0 mm` — existing `T-043` — revealed cleanly only once Applied/Pending).
        - **When user chose `"Slicer settings only"` in Step 2:**
          - Design fixes are **still shown** (never silently hidden!) with an extra muted tag `"Changes model"`.
          - Clicking **`Apply`** pops a crisp 1-time confirmation modal: `"This changes your 3D model geometry (not just slicer settings). You can undo it anytime."` → **`Apply anyway`** / **`Cancel`**.
        - **When no automated fix exists:**
          - Problem is still listed honestly with its CAD measurement (`"No automatic fix. In CAD: open this slot to 0.30 mm (now 0.15 mm)."`).
      - Quiet secondary link at bottom of expanded row: `"Accept this issue"` (marks it acknowledged so users can proceed even if they choose not to fix cosmetic warnings).
- **Pending Design Changes Sticky Bar (appears above footer whenever `≥ 1` design fix is pending):**
  - Clean tinted bar (`var(--accent-soft)`):
    - `"3 design changes pending"` (no heavy background recompute happened while picking!)
    - **`Discard`** (ghost) · **`Confirm all changes`** (primary)
    - Compact checkbox/toggle option inside confirm popover or inline: `☑ Quick re-check after applying` *(default on; unchecking applies geometry without waiting for re-check, matching feedback)*.
- **What `"Confirm all changes"` executes:**
  1. Runs `web/geometry.js` (`manifold-3d`) edit-by-edit with existing `T-434` safety checks (watertight, volume sign, piece count, bounded outer growth). Any broken individual edit is safely skipped with a plain toast naming which one and offering its CAD number instead.
  2. Saves a new version onto the Undo/Redo stack (sharing buffers per `T-460`).
  3. **If Quick re-check is on (default):**
     - Re-runs **strength check** at the *current* orientation & settings (1 fast FEA solve, ~3–5 s) and compares safety factor against the goal floor and previous safety.
     - Re-runs **print checks & heat/warp** on the modified mesh.
     - Runs a lightweight **orientation sanity check** in the background: if another orientation is now dramatically better (> 30% safer or half the support), shows a non-intrusive toast `"A different orientation is now much better — view options?"` without forcibly moving the part.
     - **If new problems appeared or safety dropped below the floor:** stays on Step 10, displays the `"Your changes caused N new problems"` banner, tags new findings with a **`New`** pill, and lets the user fix, undo, or accept them.
     - **If all clear (or user accepts remaining):** highlights **`Continue to Done →`** in the footer.
- **Slicer fixes:** Apply instantaneously (update settings map only, no geometry rebuild, immediate badge `"Applied to 3MF"`).
- **Footer:** Back · **`Continue to Done →`** (if pending design changes exist, clicking Continue prompts `"Confirm or discard your 3 pending changes first"`).

---

### Step 11 — Done & Optional Extras

- **Right Panel (`384px`):**
  - Header: `"Your part is ready"` · `"Export to your slicer or fine-tune optional details."`
  - **1. Summary Card:**
    - Clean 2×3 stat grid: `Material`, `Nozzle / Layer`, `Walls / Infill`, `Print time`, `Plastic (+ support)`, `Safety band` (plus `Cost` & `Spool warning` if configured in Settings).
    - Expandable `"Slicer settings diff (6 changes)"` disclosure showing exact key changes against goal/user's imported profile, with per-row export ticks (`T-427`).
  - **2. Primary Export Block:**
    - Slicer target `SegmentedControl` / `Select`: **`OrcaSlicer` | `Bambu Studio` | `PrusaSlicer` | `Cura`** (remembered).
    - Big primary button: **`Export 3MF`** (includes orientation, per-object settings, modifier zones, height-range fine layers, seam blockers, pauses).
    - If geometry was modified: secondary button **`Download modified STL`** + **`STEP`** (when source was STEP, via `stepwrite.js`).
    - Quiet utility row: **`Save project`** · **`Share HTML report`** · **`Copy CAD checklist`** (if CAD advice exists).
  - **3. Optional Extras (`"Want to go further?"`):**
    - Clean clickable action cards (each opens a focused full panel + 3D mode with a top `"← Back to summary"` link):
      1. **`Tolerances & fits`** — *"Size holes, pins, screws, and gaps in 3D."* (always available, including Quality)
      2. **`Make it lighter`** — *"Remove unused plastic while keeping strength."* (runs `api.topology`; **hidden for Quality goal** and hidden if no loads were set)
      3. **`Tuned settings & G-code`** — *"Cooling, fan, and speed adjustments from heat simulation."*
         - Today: lists recommended height-range fan/speed/layer-time adjustments & exports them into 3MF (`T-479`), OR rewrites uploaded/local G-code once `T-481` lands, OR exports ready-to-print G-code directly once browser slicing (`T-127`) lands.
      4. **`More tools ▸`** (collapsed disclosure housing existing specialist utilities so they stay accessible without cluttering the finish line):
         - Manual modifier zones & hidden gaps (`volume_at`)
         - Split part into pieces (`api.split`, with `"Your slicer can cut too"`)
         - Copies on the plate (`api.plate`, with `"Your slicer can arrange plates too"`)
         - Electronics box heat (`api.box_heat`)
         - Version history & A/B compare

---

## 4. Tolerances & Fits — 3D Interactive Concepts

Replacing the old `"text + bracket input"` table completely. All interaction happens **directly on the 3D model**:

### Idea 1 (Build First — Primary): Click-a-Hole / Peg → Animated Ghost Mate
1. **Hover:** Engine-detected round holes, bosses/pegs, and parallel slots highlight softly in cyan with a tiny floating diameter/width tag (`"Ø5.0 mm hole"`).
2. **Click a hole or peg:**
   - A translucent matte-steel **ghost mating pin** (for a hole) or **ghost socket collar** (for a peg) appears aligned with the axis and **smoothly animates sliding in** (respects `prefers-reduced-motion`).
   - A colored **clearance ring** highlights the radial gap between the printed part and the ghost mate (`Red` = will jam / too tight for your printer's measured error, `Amber` = tight press fit, `Green` = fits smoothly), labelled right in 3D: `"0.18 mm gap (measured on your P1S)"` or `"0.18 mm gap (typical PLA)"`.
   - A compact `Popover3D` opens beside the hole with two tabs:
     - **Tab A — Fit:** 3-stop `SegmentedControl` (**`Press` | `Snug` | `Loose`**) + mating part diameter `NumberField` (prefilled with detected nominal size, e.g. `5.0 mm`). Switching stops immediately updates the ghost pin clearance ring!
       - For `Press` fit on upright holes with enough wall room: offers a 1-click toggle **`Use grip fins`** (`T-335` crush ribs previewed right inside the hole!).
     - **Tab B — Screw / Insert:** Quick icon pills (`M2`–`M6`, auto-guessed from hole diameter!) + use (`Screw into plastic` · `Heat-set insert` · `Through bolt` · `Nut pocket`). Shows a translucent ghost screw/insert/nut sitting inside the hole!
   - **Apply to identical holes:** If 3 other holes share the same diameter (`"Apply to all 4 × Ø3.0 mm holes"` checkbox, ticked by default — reusing `T-437`).
   - **Output:** Applies `xy_hole_compensation` (slicer setting) when `"Slicer settings only"`, or queues a pending hole/boss/pocket/fin design edit (with `"Confirm all changes"` bar) when design changes are allowed.

### Idea 2 (Build Second): Printed-vs-CAD Exaggerated Shrinkage Overlay
- Toggle button in the Tolerances 3D HUD: **`Show printed shape (×5 | ×10)`**.
- Uses the user's calibrated `hole_error_mm`, `outer_error_mm`, and material `shrinkage_pct` (plus elephant foot at `z = 0`) to render a translucent orange outline of **what the printer will actually produce** over the crisp gray CAD model.
- Visually demonstrates *why* a 5.0 mm hole comes out at 4.75 mm — instant "aha!" moment for hobbyists with zero paragraphs of text.

### Idea 3 (Build Third): Two-Part Assembly Clearance Heatmap
- Button in the Tolerances panel: **`Check against mating part…`** (loads a second STL/3MF via existing `api.clearance`, `T-348`).
- Renders the second part semi-transparently in place, paints contact faces with a red/amber/green clearance heatmap, marks the tightest pinch point with a 3D callout (`"0.08 mm gap — will fuse"`), and offers 1-click compensation.

### Idea 4 (Build Fourth): Animated Feeler Gauge for Flat Gaps & Print-in-Place Joints
- Clicking any flat gap (`gap_fuses` / `flat_gap_fuses` or two opposing parallel faces via `set_gap`) slides a thin translucent feeler-blade through the slot; areas narrower than the printable clearance flash red where the blade jams, with a 1-click **`Open gap to 0.30 mm`** fix (`T-439`).

---

## 5. Quality Goal Path & Settings / Calibration Hub

### 5.1 Quality Goal Path (detailed)
- Selected in **Step 2 (Setup)** as the 5th goal card (`Quality`).
- **Flow changes:**
  - Steps **4 (Holds), 5 (Loads), 6 (Load details)** are removed from the left rail and skipped completely.
  - Step 3 (Environment) stays active (still determines material compatibility like heat/UV/water).
  - Step 7 (Analyse) offers **Fast** and **Extended** only (**Complete** is hidden because structural design additions aren't wanted).
- **Optimizer behavior for Quality:**
  - Zero FEA strength checks. Baseline structure: **2 walls, 10% infill** (per feedback).
  - Scoring prioritizes:
    1. Minimal support volume & **zero supports on prominent/visible surfaces** (`scar_area` heavily penalized).
    2. Minimal staircase artifacts (`shallow_slope_stairs`), fine detail legibility (`fine_detail_rough`, `side_detail_small`), and clean seam placement (scarf seam on round profiles, hidden corners elsewhere).
    3. Reliable printing without deformation, delamination, or inconsistent cooling (`heat.py` / `warp.py` / thermal tier A).
    4. Finer layer heights (`0.12–0.16 mm` on `0.4 mm` nozzle, or suggesting `0.2 mm` nozzle for miniatures/tiny text).
- **Problems & Extras for Quality:**
  - Design fixes focus strictly on printability & aesthetics (e.g., gentle edge rounding/chamfers where overhangs droop, sacrificial bridges) — never structural ribs/thickening.
  - Step 11 Done: keeps **Tolerances & fits** (fittings still matter on visual parts) and **Tuned settings / G-code**; hides **Make it lighter** and structural reinforcement tools.
- **Dedicated Quality Research Task (`T-562a`):** Survey and source optimal consumer-FDM quality parameters across OrcaSlicer / Bambu Studio / PrusaSlicer (outer wall speed & acceleration, inner-before-outer vs. outer-before-inner wall order, ironing flow & spacing, bridge speed & fan, scarf seam thresholds, overhang slowdown curves) and add sourced entries to `core/fdmcore/data/thresholds.yaml` + `rules.yaml`.

### 5.2 Settings & Calibration Hub
Opened from the Start screen card, the top header gear icon, or Step 2's inline `"Manage"` button. Opens as a clean overlay/view with a **Back to workflow** button (preserving all unsaved work in the active part!).

- **Layout:** `220px left tab rail | 1fr scrolling content (max-width: 780px)`.
- **Tab 1 — My Printers:**
  - List of user's printers + button **`+ Add printer`**:
    - Pick from built-in presets, OR search the shipped **415+ OrcaSlicer printer catalog** (`T-451`), OR use the 3-question closest-match wizard (enclosed? bed size? direct drive?).
  - Per-printer card fields (clean 2-column form): Name, Bed X/Y/Z, Enclosure (`Open / Passive / Heated`), Hardened nozzle toggle, Bed moves in Y (`Bed slinger`), Build plate surface (`Textured PEI / Smooth PEI / Glass / Cool plate`), Available nozzles (`0.2 / 0.4 / 0.6 / 0.8 / 1.0` checkboxes).
  - **Import Slicer Print Profile** (`T-428`): Drop OrcaSlicer / Bambu `.json` or PrusaSlicer `.ini` process profile → stores baseline speeds, walls, infill, ironing speed, retraction so Step 11 shows true diffs against *their* profile.
- **Tab 2 — My Materials:**
  - **"Filaments I own" checklist** (quick toggles for PLA, PLA+, PETG, ABS, ASA, PC, PA-CF, TPU + custom filaments). Owned materials get priority in Step 8 Options!
  - **`+ Add custom filament`** or **`Import OrcaSlicer filament .json`**.
  - Per-material editable fields: Price per kg (for cost estimates), Grams left on spool (for `spool_short` warnings), Max volumetric flow, Nozzle/Bed/Fan temps, plus sanity-check warnings (`T-333`).
- **Tab 3 — Calibration Prints:**
  - Clean 2-column grid of calibration test cards (each showing a `Measured` green pill or `Using typical` gray pill):
    1. **One-plate starter pack** (combined quick calibration plate, existing `T-446`)
    2. **Tolerances & fits** (peg + holes gauge → updates `fit_gap_mm`, `hole_error_mm`, `outer_error_mm`)
    3. **Overhang angle** (40°–70° fan test → updates `max_overhang_deg`)
    4. **Bridging** (10–50 mm spans → updates `max_bridge_mm`)
    5. **Warping bar** (40–160 mm bars → updates `warp_length_mm`)
    6. **Layer strength** (flat vs. upright break bar → updates `z_strength_factor`)
    7. **Ironing & surface finish** *(new for Quality)* (stepped ironing flow/speed tiles → stores tuned ironing baseline)
    8. **Temperature tower** *(new for Quality & Thermal)* (visual segment picker → stores optimal temp)
    9. **Thermal sag & snap tests** *(from `T-482`)* (tunes `[TO]` thermal window from `"rough"` to `"tuned"`)
  - Clicking any card opens a clean focused page:
    - 1-line explanation of what it improves
    - **`Download test model (STL/3MF)`** button
    - 3–5 short numbered print & measurement steps with simple vector illustration
    - **Visual result picker** (e.g., click the loosest hole that still holds the peg, or click the best-looking overhang angle / tower segment) → **`Save to browser`** (auto-creates/updates `"My PLA on Bambu P1S"` without forcing manual preset creation).
- **Tab 4 — Active Values ("What the checks use now"):**
  - Scannable summary table showing every active value for the current Printer + Material pair, tagged **`Measured by you`** (green) vs. **`Typical default`** (gray), with a per-row `Reset` link and a bottom **`Export / Import all my settings (JSON)`** backup button (bonus quality-of-life feature so clearing browser data doesn't wipe calibrations!).

---

## 6. Implementation Plan (Phases, Tasks, Files, Dependencies, Tests)

Engine work (**Phase 2**) runs **in parallel** with the UI Mock Gate (**Phase 1**) so nothing stalls waiting on visual sign-off. Every task is sized for one focused branch (`task/T-5XX-...`) per `AGENTS.md`.

### Phase 0 — Immediate Triage on `main` (1 small PR, ~0.5 day)
- **T-500** Pin header top (`T-491`), strip `@media (max-width: ...)` phone rules & phone e2e passes, hide Expert mode & level switch in current UI so desktop stops breaking while rebuild runs.
  - *Files:* `web/style.css`, `web/index.html`, `web/app.js`, `tools/e2e_smoke.py`, `PRODUCT.md`.
  - *Test:* `python tools/e2e_smoke.py --quick`.

### Phase 1 — Design System & Static Interactive Mocks (Owner Approval Gate)
- **T-510** New token foundation (`web/tokens.css`, `web/style.css` reset + layout grids + light/dark token hooks) + automated spacing lint test.
  - *Files:* `web/tokens.css` (new), `web/style-v2.css` (new, swapped at cutover), `core/tests/test_css_tokens.py` (new).
  - *Test:* `pytest core/tests/test_css_tokens.py`.
- **T-511** Reusable UI component library (`web/ui/*.js`: Button, ChoiceCard, SegmentedControl, Chip, Select, NumberField, Disclosure, RadialMenu, Popover3D, OptionColumn, FindingRow, ProgressCard, Toast, Modal) + interactive gallery (`design/gallery.html`).
  - *Files:* `web/ui/*.js` (new), `design/gallery.html` (new).
  - *Test:* Keyboard/focus check + screenshot of gallery at `1280×720` and `1440×900`.
- **T-512** Static HTML mocks of all screens (Start, Part, Setup, Environment, Holds 3D mockup, Loads 3D gizmo mockup, Load details, Analyse, Options 4-column + Constraints bar, Print Simulation, Problems & pending fixes, Done, Tolerances 3D, Settings Hub) → headless screenshots at `1280×720` and `1440×900` → **Owner review & approval gate before Phase 3**.
  - *Files:* `design/mocks/*.html` (new), `tools/screenshot_mocks.py` (new).
  - *Deps:* T-510, T-511.

### Phase 2 — Engine Foundations (Runs in Parallel with Phase 1!)
- **T-520** Environment material metadata & nozzle/layer-height extensions:
  - Add sourced environment fields (`water_resistance`, `food_notes`, `abrasion_resistance`, `oil_fuel_resistance`, `solvent_resistance`, `skin_safe`) and `"1.0"` nozzle entry to `presets.yaml`.
  - Allow optional `layer_height` override on `Profile` and add sourced `layer_bond_factor(lh, nozzle)` in `strength.py` (with unit tests verifying monotonic rise to ~0.55× nozzle and penalty >0.75× nozzle).
  - *Files:* `core/fdmcore/data/presets.yaml`, `core/fdmcore/profile.py`, `core/fdmcore/strength.py`, `core/fdmcore/api.py` (`material_advice`), `core/tests/test_presets.py`, `core/tests/test_strength.py`.
  - *Test:* `pytest` + `node tools/pyodide_check.mjs`.
- **T-521** Feature-edge & inside-corner geometry export for 3D hover picking:
  - Expose compact edge polylines (`feature_edges` + `concave_edges` from `regions.py` / `features.py`) and per-triangle region IDs via `api.regions()` so Three.js can do instant hover highlighting and submit edge polylines as `points` loads/holds to `strength.py`.
  - *Files:* `core/fdmcore/regions.py`, `core/fdmcore/api.py`, `core/tests/test_regions.py`.
  - *Test:* `pytest` + `node tools/pyodide_check.mjs`.
- **T-522** Unified Optimizer (`core/fdmcore/optimizer.py` + `api.optimize()` + MCP tool):
  - Goal safety floors (`5.0 / 3.0 / 2.0 / 1.5`, bumped for creep/impact), goal objectives with support-gram weighting, overhang-cached sliced `support_stats`, analytical O(1) material sweep, warm-started shell climbs, nozzle gating by wall thickness & part size, Pareto + 2–4 diverse option picker, constraint locking, honest infeasible diagnostics, and **partial best-so-far progress emission** (`Stop & use best so far`).
  - Register `optimize` in `api.METHODS` **and** `mcp_server.py` in the same PR; run `python tools/make_manifest.py`.
  - *Files:* `core/fdmcore/optimizer.py` (new), `core/fdmcore/api.py`, `core/fdmcore/mcp_server.py`, `core/fdmcore/manifest.json`, `core/tests/test_optimizer.py` (new).
  - *Deps:* T-520.
  - *Test:* Corpus tests for every goal + constrained re-opt + infeasible test + `test_every_api_method_has_a_tool` + `pyodide_check.mjs`.
- **T-523** Complete-depth Pyodide-safe local reinforcements + Runtime estimator:
  - Add voxel-space `Zone` modifier, `void` hidden-gap, and local fillet/thickening scoring to `optimizer.py` (emitting edit meshes for JS `manifold-3d` without calling `LOCAL_ONLY` `booleans.py`).
  - Calibrate Fast / Extended / Complete runtime estimates (`api.estimate_runtime()`) on the corpus.
  - *Files:* `core/fdmcore/optimizer.py`, `core/fdmcore/api.py`, `core/tests/test_optimizer.py`.
  - *Deps:* T-522.
- **T-524** Concise Findings & Finding-to-Edit linking:
  - Add `short_title` (≤ 6 words), `one_liner` (≤ 80 chars), `instance_label`, and stable `finding_id` on every finding; link `design()` edits directly to `finding_id` / `bbox`; keep full `detail` for `"More"`. Ensure `rules.yaml` short titles stay punchy.
  - *Files:* `core/fdmcore/checks.py`, `core/fdmcore/features.py`, `core/fdmcore/data/rules.yaml`, `core/fdmcore/api.py`, `core/tests/test_advice_sanity.py`.
  - *Test:* `pytest` asserting title length ≤ 6 words and 1-to-1 instance mapping on multi-hole corpus parts.

### Phase 3 — New Modular Shell, Early Steps & Settings Hub
- **T-530** Clean modular frontend foundation: `web/state.js` (central store + Undo/Redo + IndexedDB autosave), `web/flow.js` (declarative step list with goal skip rules), `web/shell.js` (52px header, rail, stage container, inspector container, wide-split mode), `web/messages/en.js` (new i18n keys).
  - *Files:* `web/state.js`, `web/flow.js`, `web/shell.js`, `web/messages/en.js`.
  - *Deps:* T-512 approved.
- **T-531** Screen 0 (Start screen with 2 main cards + Settings card + Recent projects) & Step 1 (Your Part drop zone, Sample popover, auto-advance, multi-body picker, unit-scale banner, broken-mesh toast).
  - *Files:* `web/steps/start.js`, `web/steps/part.js`.
  - *Deps:* T-530.
- **T-532** Step 2 (Setup: Printer/Material/Nozzle with `"Decide later"`, 5 goals, what-can-change) & Step 3 (Environment: temperature cards + contact chips + live compatible materials pill).
  - *Files:* `web/steps/setup.js`, `web/steps/environment.js`.
  - *Deps:* T-520, T-530.
- **T-533** Settings & Calibration Hub (Printers + 415 catalog + profile import, My Materials owned checklist + filament import, Calibration test print pages, Active Values table + JSON backup). Built **here** so Step 2's `"Manage"` button works immediately.
  - *Files:* `web/steps/settings.js`, `web/mypresets.js`.
  - *Deps:* T-530, T-532.

### Phase 4 — 3D Spatial Interactions (Holds, Loads, Load Details)
- **T-540** GPU-friendly 3D Hover Picker in `web/viewport/picker.js`: triangle→region lookup, screen-space 6 px priority for feature edges & inside corners, translucent highlight + crisp edge outline, `rAF` throttling, X-ray mode (`X` key).
  - *Files:* `web/viewport/picker.js` (new), `web/viewer.js`.
  - *Deps:* T-521, T-530.
- **T-541** Step 4 (Holds): `RadialMenu` anchored to 3D click (face / hole / edge / inside corner / brush), cobalt lock markers, over-constraint toast (`≥ 4` holds or `> 30%` area), right-panel list with `Fixed / Resting / Screw` inline toggle, `"Skip strength"` escape hatch.
  - *Files:* `web/steps/holds.js`.
  - *Deps:* T-540, T-511.
- **T-542** 3D Direction Gizmo (`web/viewport/gizmo.js`): normal Push/Pull + world ±X/±Y/±Z quick-pick arrows + 3-axis slicer-style rotation rings (`15°` snap, `Shift` free, read-only angle readout).
  - *Files:* `web/viewport/gizmo.js` (new), `web/viewer.js`.
  - *Deps:* T-540.
- **T-543** Step 5 (Loads) & Step 6 (Load Details): Gizmo → `Popover3D` force input (`N | MPa` with live area & kg conversions), log-scaled persistent 3D arrows, right-panel list + optional extra load cases, Step 6 duration/temp-confirm/vibration-knocks.
  - *Files:* `web/steps/loads.js`, `web/steps/load_details.js`.
  - *Deps:* T-542.

### Phase 5 — Optimizer & Options UI
- **T-550** Step 7 (Analyse): Depth cards with part-size time estimates, live stage `ProgressCard`, `Cancel` (with full state restore), and `"Stop & use best so far"`.
  - *Files:* `web/steps/analyse.js`, `web/worker.js` (streaming partial results).
  - *Deps:* T-522, T-523, T-543.
- **T-551** Step 8 (Options): Wide split layout, 2–4 `OptionColumn`s with Pros/Cons & aligned metrics, hover-orientation 3D sync, stress/reinforcement toggle, sticky **My constraints** bar with editable chips, **`Re-optimize with my choices`**, and Infeasible recovery banner.
  - *Files:* `web/steps/options.js`.
  - *Deps:* T-550.

### Phase 6 — Simulation, Problems & Fixes, Done
- **T-560** Step 9 (Print simulation): Setup confirmation + room/bed/enclosure inputs + optional G-code upload + runs `checks` + `heat` + `warp` + 3D overlay switcher (`Normal / Heat / Warp / Internal Stress / Overhangs`) + clean hook for future `[TO]` Thermal Tier A (`T-473`–`T-476`).
  - *Files:* `web/steps/simulate.js`.
  - *Deps:* T-551.
- **T-561** Step 10 (Problems & Fixes): Severity-grouped `FindingRow`s (1 per instance), `More` disclosure, Fixes directly underneath (`Slicer / Design / Process`), `"Changes model"` confirm modal when slicer-only, immediate ghost previews, sticky `"N pending · Confirm all changes · Discard"` bar, `manifold-3d` safe apply (`T-434`), optional quick re-check + `New` regression badges + `"Accept remaining"`.
  - *Files:* `web/steps/problems.js`, `web/geometry.js`.
  - *Deps:* T-524, T-560.
- **T-562** Step 11 (Done & Export): Summary card, settings diff with export ticks, slicer selector + **`Export 3MF`** + STL/STEP + Save project + Share HTML report + Optional Extras cards + `"More tools"` drawer (split, copies, box heat, manual zones, versions).
  - *Files:* `web/steps/done.js`.
  - *Deps:* T-561.

### Phase 7 — Quality Goal Path, 3D Tolerances & Optional Extras
- **T-570** 3D Tolerances & Fits (Ghost Mate slide-in + clearance ring + Fastener quick-pick + Printed-vs-CAD exaggerated shrinkage toggle; later mating-part heatmap & feeler gauge).
  - *Files:* `web/steps/tolerances.js`, `web/viewport/tolerances3d.js`.
  - *Deps:* T-540, T-562.
- **T-571** Make It Lighter (`api.topology` sub-view reachable from Done; hidden for Quality) + Tuned Settings / G-code sub-view (height-range modifiers in 3MF now; hooks for `T-479`/`T-481`/`T-127`).
  - *Files:* `web/steps/lighter.js`, `web/steps/tuned_gcode.js`.
  - *Deps:* T-562.
- **T-572** Quality Goal Path & Research:
  - `T-572a`: Research sourced quality settings (outer wall speed, ironing, bridge speed, scarf seams, wall order) → `thresholds.yaml` + `rules.yaml` + ironing & temp-tower calibration test models.
  - `T-572b`: Wire Quality objective in `optimizer.py` (2 walls, 10% infill, surface/scar minimization) and end-to-end Quality flow in UI.
  - *Files:* `research/quality_settings.md`, `core/fdmcore/optimizer.py`, `core/fdmcore/data/thresholds.yaml`, `web/steps/settings.js`.
  - *Deps:* T-522, T-533, T-562.

### Phase 8 — Cutover, Dead Code Removal & Verification
- **T-580** Cutover `web/index.html` & `web/style.css` to the new shell; delete legacy `web/app.js` monolith, old `web/steps.js`, old panes, and unused CSS/DOM.
- **T-581** Rewrite `tools/e2e_smoke.py` (desktop `1440×900` & `1280×720`) covering: Start → Part (sample + STEP) → Setup → Environment → Holds (face + edge + toast) → Loads (gizmo + N/MPa) → Load details → Analyse → Options (pin PLA + re-optimize) → Simulation → Problems (pending design change + Confirm all) → Done (3MF export) → Tolerances 3D → Quality path → Settings Hub → Cancel & Undo/Redo.
- **T-582** Update `PRODUCT.md`, `DESIGN.md`, `PLAN.md`, `README.md`, and `agents/` skill/snippets for `optimize()` and the new flow.

*Tasks in `TASKS.md` superseded and closed by this redesign:* Navigation rebuild, Phone layout (`K`), old 4-level step wizard items, old strength form UI items. All `[TO]` Thermal Optimizer tasks (`T-472`–`T-484`) continue uninterrupted and plug straight into Step 9 and Step 11.

---

## 7. Open Questions for the Owner

Only the 6 decisions that genuinely change the product or roadmap (each with a recommended default):

1. **Browser slicing & G-code (`T-127`):** Your feedback mentions using an internal OrcaSlicer engine in the browser to go straight to G-code. Right now, OrcaSlicer CLI only runs in **local mode** (`fdmdfm`), and the browser WASM slicer (`Muon3D`, ~21 MB download) was postponed while pre-release.
   - *Recommendation:* Ship the redesign first with our existing browser engine (pseudo-slice + `heat.py` + `warp.py` + 3MF export with per-object/height-range settings, plus optional G-code upload / local mode for real G-code), and plug browser WASM slicing into Step 9 & Step 11 once a stable release is ready. Or do you want us to revive browser WASM slicing right away?
2. **Non-loaded parts on Balanced / Fast / Light:** If someone drops a decorative or lightly-loaded part (like a tray or organizer) and picks Balanced, Fast, or Light, should Step 4 (Holds) include a quiet `"No mechanical load — skip strength"` link, or should non-loaded parts always use the **Quality** goal?
   - *Recommendation:* Include the quiet skip link on Balanced/Fast/Light (not Strong) so beginners aren't forced to invent fake forces.
3. **Visual theme (Light vs. Dark CAD workbench):** Since we are throwing out the old green-tinted styling completely, do you prefer a **crisp neutral light theme** (white/cool-gray surfaces, blue accent), a **dark CAD studio theme** (like Bambu Studio / Plasticity, where 3D parts and stress colors pop), or **light default with a dark mode toggle**?
   - *Recommendation:* Crisp neutral light default with a clean dark mode toggle in the header/Settings (easy to build cleanly with CSS tokens from Day 1).
4. **3D Tolerances priority:** Which of the four 3D tolerance visualizers should we build first in Phase 7?
   - *Recommendation:* **#1 Animated Ghost Mate** (click hole/peg → translucent pin/screw slides in with live red/amber/green gap ring) + **#2 Printed-vs-CAD exaggerated shrinkage overlay**, then mating-part heatmap (#3) and feeler gauge (#4).
5. **Large-nozzle threshold (`0.8 / 1.0 mm`):** We propose offering `0.8` / `1.0 mm` nozzles only when: (a) the printer supports them, (b) the part's thinnest wall is at least `2 × line width` (so details don't disappear), and (c) the part is large (`≥ 120 mm` longest side **or** `≥ 100 cm³` solid volume). Does that rule feel right, or would you prefer a different cutoff?
6. **Home for existing specialist tools:** Does placing *Split part (too big / less support)* and *Pause-to-insert* directly under their Step 10 findings, and placing *Copies on plate*, *Electronics box heat*, *Manual modifier zones*, and *Version A/B compare* inside a collapsed `"More tools"` card on Step 11 Done work for you — or should any of those be promoted or dropped?
