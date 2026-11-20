# Redesign plan (Claude draft) — from feedback round 5, 2026-11-21

Source: [feedback.md](feedback.md) (verbatim). This draft is one of three; GPT-6.2 Sol and Gemini 4 Neon
review it and write their own, then the three are merged into `PLAN-COMBINED.md`.

Every point below is tagged with where it comes from in the feedback (**[F]** = said directly,
**[I]** = my inference / proposal, needs a yes). Open questions are collected in section 20.

---

## 0. What changes, in one paragraph

The app stops being a pile of panes with levels (Simple / Basic / Advanced / Expert) and becomes **one
guided desktop workflow** that collects what the user wants (part, printer, goal, what may change,
environment, holds, loads), then **does the heavy optimizing by itself** (orientation, settings,
material, nozzle, optionally design additions; minutes are fine), presents **2–4 complete options as
columns** to pick from or constrain, then simulates printing, lists the **remaining problems one by
one with their fixes right underneath**, and ends with optional extras (tolerances, lighter, tuned
G-code) and export. Everything spatial (holds, loads, directions, tolerances) happens **in the 3D view,
at the mouse**, not in side forms. The visual layer is rebuilt from scratch with one spacing scale and a
small component set. Phone layouts and the Expert scroll page are removed.

---

## 1. Global rules (apply to every screen)

### 1.1 Desktop only [F]
- Delete every phone/tablet rule: all `@media (max-width: …)` blocks in `web/style.css`, mobile-only
  markup, bottom sheets, phone tab bars, touch-only code paths, the "on a phone the site has to…" promise.
- Minimum supported viewport: **1280 × 720** [I]. Below that the page simply gets scrollbars; no layout
  switches. No `max-width` breakpoints at all.
- `PRODUCT.md` "Operating Context" is rewritten: desktop only, no phone use case (also drop the
  "read a shared report on a phone" promise; the HTML report file can still open anywhere, untested).
- The test `e2e_smoke.py` drops its phone-viewport passes.

### 1.2 Header always on top [F]
- App shell is a CSS grid with explicit rows: `header (56 px) / main (1fr)`. The header is
  `position: sticky; top: 0` inside its own row, so nothing injected into `<body>` (extensions,
  toasts, dialogs) can push it down. This is T-491 (already claimed); the rebuild must keep the rule.
- Header contents, left to right: brand · current workflow name ("Optimize a print") · step breadcrumb
  (compact) · spacer · **Export 3MF** (enabled once a part is loaded) · **Settings & calibration** (gear) ·
  Help. **No level/mode switch** [F].

### 1.3 Levels and Expert mode [F]
- Remove the level switch from the UI. Hide `expert` entirely (code path behind a dead flag, then
  deleted). Simple/Basic/Advanced are collapsed into the single flow below; "we split it later or
  make sections optional" [F].
- Note for later [F]: when levels return, **Expert = same structure as Advanced** (same steps, more
  controls visible), never the long scroll page.
- `web/steps.js` `LEVEL_STEPS`/`MOVED` are replaced by one ordered step list with "optional" and
  "skipped for goal X" flags.

### 1.4 Writing rules (the "walls of text" fix) [F]
- Every control: **label (≤ 4 words) + optional one-line hint (≤ 80 chars)**. Nothing else visible.
- Every finding: **title ≤ 6 words** ("Side hole prints oval"), one-line consequence, then a **More**
  disclosure for the explanation, numbers and sources.
- No sentences between buttons. No parenthetical asides in labels ("(or saved project)" removed [F]).
- One item per instance: three oval holes = three findings, each pointing at its hole [F].
- All text through `t("key")` (`web/messages/en.js`) per AGENTS.md; the rewrite is the moment to move
  every string.

### 1.5 Visual system, rebuilt [F + I]
Don't restyle the old CSS; write a new `web/style.css` from tokens up.
- **Spacing scale (only these):** 4, 8, 12, 16, 24, 32, 48 px as `--s1 … --s7`. Rule: inside a
  control 8, between controls in a group 12, between groups 24, between sections 32. A lint test greps
  `style.css` for raw px margins/paddings outside the token file and fails.
- **Type scale:** 13 / 15 / 17 / 20 / 28 px, two weights. Keep Atkinson Hyperlegible.
- **Layout grid of the workflow screens:** `left step rail 220 px | 3D stage 1fr | right panel 400 px`.
  The right panel has a fixed header (step title + one-line purpose), a scrolling body, and a fixed
  footer with **Back** (ghost) and **Next / Run** (primary). Next is always at the same spot [F: "Next is
  always on the side"].
- **Component set (all new, one file each under `web/ui/`):** Button (primary / secondary / ghost /
  danger), SegmentedControl, ChoiceCard (big selectable card with icon, title, one line), Chip
  (multi-select), Select (styled, searchable, with "Decide later" option), NumberField with unit
  switch, Disclosure ("More"), Toast (bottom-center info, auto-dismiss), Popover/RadialMenu
  (anchored to a 3D point), OptionColumn, FindingRow, ProgressCard (what runs, how long, cancel), StepRail.
  No raw `<select>`/`<input>` styling left to the browser.
- Colour: keep DESIGN.md tokens as a starting palette but re-derive surfaces; one accent, status colours
  only for status.
- **Design review gate:** each screen is mocked as a static HTML page first (`design/mock/*.html`),
  screenshotted and approved by the user **before** wiring logic [I].

---

## 2. The flow at a glance

```
Start ─┬─ Optimize your print ─► 1 Part ─► 2 Setup ─► 3 Environment ─► 4 Holds ─► 5 Loads ─► 6 Load details
       │                                                                              │
       │                                   (goal = Quality skips 4–6 and strength)    ▼
       │                                                              7 Analyse (Fast / Extended / Complete)
       │                                                                              ▼
       │                                                              8 Options (columns, constrain, pick)
       │                                                                              ▼
       │                                                              9 Print simulation (heat, cooling, warping)
       │                                                                              ▼
       │                                                             10 Problems & fixes (pending → Confirm all)
       │                                                                              ▼
       │                                                             11 Done → optional: Tolerances · Lighter ·
       │                                                                     Tuned slicer settings / G-code
       ├─ Fix your broken prints  (Coming soon card)
       └─ Settings & calibration  (header gear; also linked from Start)
Export 3MF: header button, available from step 2 on.
```

---

## 3. Start screen [F]

- Full window under the header. Two large cards side by side, equal size, centered:
  - **Left: "Optimize your print"** — one line: "Get the best orientation, settings and material for
    your part." Primary click target is the whole card.
  - **Right: "Fix your broken prints"** — badge **Coming soon**; below it a short bullet list of what is
    coming (e.g. "Find out why a print failed from a photo or description", "Strings, warping,
    layer splits", "Tuned G-code from your print") — the card is disabled but readable [F].
- Below the cards, small: "Recent projects" (if any) and a link "Settings & calibration" [I, see Q1].
- Header here has no breadcrumb, no Export.
- The old "What do you want to do?" dialog and the tour are removed.

## 4. Step 1 — Your part [F]

- Center (the 3D stage area) is one **big drop zone**: icon, "Drop your part here", button "Choose
  file", small line "STL, 3MF or STEP". Nothing in brackets; "or saved project" removed (projects open
  from Recent projects / File menu) [F].
- Exactly one more element: a **"Sample parts"** button under the drop zone that opens a popover grid
  of 6–8 thumbnails with names; click loads it [F]. No other sentences, no "no files at hand?" text [F].
- On load: the part shows in the stage and the flow **advances automatically to Setup** [F]. Loading
  progress appears in the drop zone itself.
- Errors (multi-body, broken mesh) show as one Toast + a "More" with the detail.

## 5. Step 2 — Setup ("What you have") [F]

Right panel, three groups, top to bottom:

1. **Printer** — searchable Select (preset list + the user's own printers). Beside it a gear button
   "Manage printers & materials" that opens the Settings & calibration hub on the printers tab [F].
   **Material** Select next to it, default **"Decide later"** [F]. **Nozzle** Select, default
   **"Decide later"** [F]. "Decide later" means the optimizer picks (and offers alternatives).
2. **What matters most?** — SegmentedControl / ChoiceCards: **Balanced · Strong · Fast · Light ·
   Quality** [F; Quality from the later part of the feedback, see section 16]. One-line hint under the
   selected one only.
3. **What can you change?** — three ChoiceCards: **Slicer settings** · **The design** · **Both** [F].
   (Replaces the old "Just print it / I'm designing it / Both" mode.)

Footer: Next. Validation: printer required; everything else has defaults.

## 6. Step 3 — Environment [F]

Purpose (panel header line): "Where will the part be used? This decides the material."
- **Temperature** — keep the existing temperature choice (indoor / warm / hot car / outdoor cold …) [F],
  restyled as ChoiceCards with a °C range under each.
- **What will it touch?** — multi-select Chips with icons [F, list partly I]:
  - Water / wet / humid
  - Food or drinks
  - Outdoors: sun & rain (UV)
  - Dirt, sand, scraping (abrasion)
  - Oil, grease, fuel (workshop / machine shop)
  - Cleaning agents / solvents (alcohol, acetone) [I]
  - Skin contact / worn [I]
  - None of these
- These feed **material choice** and a few rules (food contact → warnings about layer gaps, material
  certification and that FDM is hard to keep food-safe; UV → ASA/PETG over PLA). Data: extend
  `core/fdmcore/data/presets.yaml` materials with `water`, `food`, `uv_resistance` (exists),
  `abrasion`, `oil`, `solvent`, `max_service_temp`, each with a source [F requirement: sources].
- Footer: Next. If goal = Quality → Next goes to step 7 (Quality path).

## 7. Step 4 — Holds ("Where is it held?") [F]

All interaction in 3D; the right panel only lists and explains.
- **Hover highlighting** before clicking [F]: as the mouse moves, the face region (or edge, or inside
  corner) under the cursor is highlighted (soft fill + outline). Priority: edge if within ~6 px screen
  distance of a feature edge, else the face region (CAD face for STEP, segmented region for STL).
- **Click → radial menu at the cursor** [F] with 2–3 items: "Hold this face", "Hold this edge"
  (when an edge is under the cursor), "Cancel". Optional later: "Hold part of this face" (brush) — from
  the earlier feedback (docx) [I].
- Confirmed holds render in a hold colour with a small lock glyph; click one → radial menu "Remove".
- **Multiple holds allowed** [F]. From the 4th hold (or > 30 % of the surface), a Toast at the bottom:
  "Hold only where it's really fixed — too many holds hide weak spots." [F]
- Right panel: list of holds (name like "Face 3 · 412 mm²"), each with remove; footer **Next**, enabled
  after ≥ 1 hold, with the hint "or add more holds" [F].
- **Removed**: "this face stays clean", "put on the bed" and every bed-related choice here [F]. Bed
  placement is decided by the optimizer later.

## 8. Step 5 — Loads ("Where does it get pushed or pulled?") [F]

Sequence at the mouse, no side forms [F]:
1. Hover highlights faces/edges as in step 4.
2. Click → a **direction gizmo** appears anchored at the clicked point [F]:
   - Arrows for the face normal (push in / pull out) and the six world axes ±X ±Y ±Z — click one to pick.
   - **Rotation rings** (like slicer rotate gizmos) to tilt the arrow freely; snapping every 15°, hold
     Shift for free. No angle number fields [F]. (A readout of the angle may show small, read-only.)
3. After the direction is set, a small **popover at the arrow** asks for the size:
   NumberField + unit switch **N | MPa** [F] (kg as a third unit? see Q4). For MPa the tool converts
   with the selected area and shows "= 1 240 N on 412 mm²" [F]. Enter / OK sets it.
4. The arrow stays on the model (length ∝ log force), labelled with the value.
- Several loads allowed [F]. Right panel lists them; each row: name, direction icon, value; click a
  row → focuses it in 3D and reopens the gizmo/popover to edit [F].
- Edge loads and point loads: clicking an edge spreads the load along the edge; Alt-click = point load
  [I, from docx feedback].
- Footer: Next (needs ≥ 1 load).

## 9. Step 6 — Load details [F]

Short panel, three questions:
- **How long does the load act?** ChoiceCards: "Short moments" · "Often, on and off" · "All the time
  (creep)" [F: "is load continuously"].
- **Temperature during use** — prefilled from step 3: "We'll check at 30 °C (outdoor summer). Right?"
  with Yes (default) / Change [F].
- **Shaking or knocks?** Yes / No toggle [F]; Yes reveals one choice (light vibration / hits and drops).
- No print-strength or layer-adhesion inputs [F].
- Footer: Next.

## 10. Step 7 — Analyse ("How deep should we look?") [F]

Three ChoiceCards with an **estimated time computed from the part size** (voxel count × measured
per-voxel cost, calibrated on the corpus) [F]:
- **Fast** — strength check, orientation sweep, a few setting combinations. ~30 s–2 min.
- **Extended** — everything except design changes: all orientations, walls/top/infill/layer height,
  nozzle sizes, materials allowed by the environment. ~2–8 min.
- **Complete** — Extended + **advanced design changes** tested automatically at weak spots (hidden
  gaps that become solid walls, added plastic, local modifiers). ~10–25 min. Only offered when "What
  can you change" includes the design (otherwise shown disabled with "needs design changes") [F+I].
- Big **Run** button. While it runs: ProgressCard in the panel (current stage, elapsed, estimate,
  Cancel), the stage shows the part with a subtle scanning overlay. The PC stays usable (one worker,
  capped threads — existing rule).

### 10.1 What the optimizer does [F]
1. **Orientation**: every candidate direction (existing candidate set incl. resting planes); one FEA
   solve rotated per direction (existing). For each: layer-direction safety, support volume → grams,
   bed contact, print time estimate. Orientation is **chosen automatically**; the old "On the bed" step
   is gone from the main flow [F].
2. **Safety targets by goal** [F]: Strong ≥ **5**, Balanced ≥ **3**, Light ≥ **1.5**, Fast ≥ **2** [I,
   Q2]. The target is a floor: candidates below it are discarded; above it the goal's objective decides.
3. **Objective by goal** [F]:
   - Strong: maximise safety; support grams nearly irrelevant.
   - Balanced: weighted sum of print time, grams (part + support), safety above floor.
   - Light: minimise total grams (part + **support**, support weight matters most here [F]).
   - Fast: minimise print time; allows bigger nozzles (0.6 for small, 0.8/1.0 for big parts [F]),
     thicker layers.
4. **Setting space** [F]: walls, top/bottom layers, infill %, pattern, layer height (thicker layers are
   stronger per layer interface — model it [F]), nozzle (≤ 0.6 for small parts; 0.8/1.0 considered
   when the part's bounding box / volume is large — threshold from data, Q6), material from the
   environment-compatible list (or fixed if the user picked one).
5. **Complete only**: weak-spot regions from the stress field → try `additions.py`/`placement.py`
   edits (hidden gaps for solid walls, plastic added, modifiers with more walls/infill), each re-solved.
6. Output: a **Pareto set**, from which 2–4 diverse options are picked (e.g. best-for-goal, cheapest
   material the user likely has (PLA), least support, fastest). Mirror-equivalent orientations collapsed.
   If nothing reaches the floor: say so plainly and show the best reachable safety + what would be
   needed (other material, design change).

Engine: new API `optimize(part, printer, goal, can_change, environment, holds, loads, load_details,
depth, constraints)` in `core/fdmcore/api.py` → wraps existing `orientation`, `strength`, `fea`,
`recommend.goal_plan`, `OPT_TARGET` (replaced by goal floors), `additions`. Streams progress stages.
Must remain Pyodide-safe (numpy/scipy/shapely/pyyaml only).

## 11. Step 8 — Options [F]

Layout: the 3D stage shows the **selected column's** part orientation; the panel widens (or the stage
splits) to show **2–4 OptionColumns** side by side [F].
- Column header: option name ("Strongest", "Least plastic", "PLA only", "Fastest"), then **Pros**
  (green, ≤ 3 one-liners) and **Cons** (≤ 3) at the top [F].
- Rows below (same rows in every column, aligned): Orientation (small thumbnail; hover → shows it in 3D),
  Material, Nozzle, Layer height, Walls, Top/bottom, Infill, Supports (grams), Weight, Print time,
  Safety (×N with traffic colour), Design changes (Complete only, count + "show").
- **Per-row checkbox "Keep"** in every column + "Keep all" in the header [F]. Row values are editable
  inline (Select / number) [F].
- Actions under the columns:
  - **Use this option** (on a column) → applies it, continues.
  - **Re-optimize with my choices** (when some rows are kept/edited) → runs the optimizer again with
    those rows fixed as constraints and returns new columns, all respecting them (e.g. all PLA) [F],
    or a clear "No way to reach ×3 with PLA here — best is ×2.1. Options: allow PETG, allow design
    changes, or accept ×2.1." [F]
- Owned materials from Settings ("My materials") pre-mark materials the user doesn't have [I, Q7].

## 12. Step 9 — Print simulation [F]

- Purpose line: "Now we simulate printing your chosen option." One **Run simulations** button [F].
- Runs: cooling / layer time, heat build-up (thermal tier A, T-473 when ready; `heat.py`/`spot_heat`
  until then), warping, and later delamination risk (T-474/T-476). Uses our own slice of the chosen
  option; when the browser OrcaSlicer engine lands (T-127/T-483) the real G-code is used [F].
- Results don't get their own report pages: they **become findings** in step 10, each with a 3D
  colour map toggle (heat / warp) on the stage [F].
- If the simulation needs extra info (enclosure, bed temp, room temp) it asks inline with defaults [F].

## 13. Step 10 — Problems & fixes [F]

- List of FindingRows, grouped by severity (Will fail · Shape off · Looks rough · Weak · Info).
- Each row: short title + one line [F]; click → expands: **More** (explanation, numbers), then
  **Fixes** directly under it [F]: each fix is a row with its kind tag (Slicer / Design / Process) and an
  **Apply** button. Problems without any applicable fix are still listed, with "No slicer fix; a design
  change would help: …" [F].
- Clicking a finding focuses its location in 3D (existing behaviour, keep).
- **Design fixes are pending** [F]: Apply shows the change as a ghost overlay on the model immediately,
  marks the fix "Pending", and **nothing is recomputed**. A sticky bar appears: "3 changes pending ·
  Confirm all changes · Discard". 
- **Design fixes when the user chose "Slicer settings only"** [F]: still shown, marked "Changes your
  model"; Apply opens a confirm: "This changes your model. You can undo it later." → then pending.
- **Confirm all changes** [F]: applies geometry, then re-checks in this order:
  1. Strength at the chosen orientation/settings: is the safety still ≥ the floor and close to before?
  2. Print checks + simulations on the changed part.
  3. Background quick sanity check (would another orientation now be much better? only flag, don't move).
  No full re-optimization [F]. If new problems appear: back to the list with a banner "Your changes
  caused 2 new problems" and those findings marked New [F]. Loop until no problems or the user accepts
  the rest ("Accept remaining" per finding).
- Slicer fixes apply instantly (settings only) and just update the settings summary.

## 14. Step 11 — Done & optional extras [F]

"Your part is ready." summary card (orientation, material, settings, safety, time, grams) +
**Export 3MF** (primary) + **Download STL/STEP** if the design changed. Then optional cards [F]:
- **Tolerances & fits** (section 15)
- **Make it lighter** ("You want it even lighter?") [F] — hidden for Quality goal [F].
- **Tuned settings from the heat simulation** → temperature, fan, speed changes listed, then
  **Download G-code** once the in-browser slicer exists; otherwise 3MF with modifiers (T-479/T-481) [F].
- **Advanced design changes** (manual placement, for users who want to place gaps/plastic themselves).
- Export 3MF stays available at any time in the header [F].

## 15. Tolerances & fits — 3D ideas [F asks for ideas]

Goal: no "text + bracket input" rows. Proposals (pick in Q8):
1. **Click-a-hole → ghost mate.** Hover highlights holes/pegs/slots. Click a hole → a translucent mating
   pin (or for a peg: socket) appears and **animates sliding in**. A three-stop SegmentedControl
   "Press fit · Snug · Loose" changes the gap; the gap is drawn as a coloured ring (red = too tight for
   your printer, green = OK) with "0.15 mm gap" label.
2. **Printed-vs-CAD overlay.** Using the user's tolerance calibration (Settings), show the predicted
   printed shape (shrunken holes, elephant foot) as an outline over the CAD shape with exaggeration
   slider (×5 / ×10).
3. **Pair two parts.** Load a second part (existing "Add another model"), drag/align it onto the first,
   the tool colours contact areas by clearance and suggests per-hole compensation.
4. **Slot/gap sweep.** For flat gaps, an animated feeler gauge slides through; red where it jams.
Output: per-hole compensation as slicer setting (hole XY compensation) when only slicer may change, or
resized geometry when design may change (pending + confirm as in step 10).

## 16. Quality goal (separate path) [F]

- Chosen in Setup. **Skips holds, loads, load details and strength entirely** [F].
- Still asks environment (temperature, contact) [F].
- Analyse step offers Fast / Extended (Complete hidden — design additions are mostly unwanted [F]).
- Optimizer objective: least support that still prints reliably, support placement away from visible
  faces, surface quality (layer lines on visible faces, seam placement), minimal deformation /
  delamination / temperature inconsistency [F]. Default structure: 2 walls, 10 % infill [F].
- Settings research needed [F]: speeds, cooling, ironing, bridge speed, outer wall speed, seam →
  new research task + calibration tests (ironing, bridge, overhang, temperature tower).
- Extras for Quality: Tolerances yes (fits matter) [F]; Lighter no, advanced design changes no [F];
  design suggestions only "round off this edge" style [F].

## 17. Settings & calibration hub [F]

Opened from the header gear (and Start). Full-page view with a left tab list:
- **My printers** — add from presets, import slicer profiles (OrcaSlicer / Bambu / Prusa — existing
  import), "My printer isn't listed" (existing). Imported profile values become baselines (speeds,
  ironing, retraction…) [F].
- **My materials** — which filaments the user owns (brand/type/colour optional) [I]; optional per-
  material tuned values (temp, flow, PA).
- **Calibration tests** — cards: Tolerance/fit test, Temperature tower, Overhang, Bridge, Ironing,
  Sag test & snap test (T-482). Each opens a page: what it measures (1 line), **Download model**,
  how to print it (3–5 numbered steps with picture), **Enter result** (visual picker, e.g. click the
  best tower segment in a 3D preview), saved in the browser [F].
- **What the checks use now** — summary of active calibration values.
- Everything stored in browser storage (existing `mypresets.js`) and in project files.

## 18. "Fix your broken prints" [F]

Only the Coming-soon card for now. The existing "My print failed" pane is hidden until that workflow is
designed (list its current content as the seed of that future plan).

---

## 19. Implementation plan (tasks)

Order matters; each is one branch from `main` per AGENTS.md. IDs proposed as T-500+.

### Phase A — clean up (small, safe, fast)
- **T-500** Remove phone layouts: delete `max-width` media queries, mobile markup/JS, phone e2e passes;
  update PRODUCT.md. Files: `web/style.css`, `web/index.html`, `web/app.js`, `tools/e2e_smoke.py`, `PRODUCT.md`.
- **T-501** Header pinned top (finish T-491) + header contents without level switch.
- **T-502** Disable Expert and the level switch; one step list. Files: `web/steps.js`, `web/app.js`.

### Phase B — design system and mocks (user approval gate)
- **T-510** Tokens + new `style.css` foundation (spacing/type/colour), spacing lint test.
- **T-511** Component library `web/ui/*.js` (section 1.5) + a component gallery page `design/gallery.html`.
- **T-512** Static mocks of every screen (Start, Part, Setup, Environment, Holds, Loads, Load details,
  Analyse, Options, Simulation, Problems, Done, Tolerances, Settings hub) → screenshots → **user review**.

### Phase C — new shell and early steps
- **T-520** App shell (header / rail / stage / panel / footer) + router for the new step list.
- **T-521** Start screen.
- **T-522** Part step with sample popover and auto-advance.
- **T-523** Setup step (printer/material/nozzle with "Decide later", goal, can-change).
- **T-524** Environment step + material environment data in `presets.yaml` (sourced) + rules for food/UV/oil.
- **T-525** Split `app.js` (5 500 lines) into per-step modules `web/steps/*.js` as they are rebuilt.

### Phase D — 3D interaction
- **T-530** Hover picking: face-region / edge / inside-corner highlight in `viewer.js` (GPU-friendly:
  precomputed region ids per triangle, edge polylines; raycast throttled to rAF).
- **T-531** Radial menu / popover anchored to a 3D point (follows on orbit, closes on Esc).
- **T-532** Holds step (multi, toast warning, list, remove).
- **T-533** Direction gizmo (axis arrows + normal + rotation rings with snapping).
- **T-534** Loads step (gizmo → force popover N/MPa with area conversion, list/edit).
- **T-535** Load details step.

### Phase E — optimizer and options
- **T-540** `optimize()` engine API: goal floors (5 / 3 / 1.5 / 2), objectives, support grams in score,
  layer height + nozzle + material in the search, Pareto + diverse pick, constraints, infeasible report.
  Tests per goal on the corpus.
- **T-541** Time estimate model for Fast/Extended/Complete from part size.
- **T-542** Complete depth: automatic advanced design additions at weak spots.
- **T-543** Analyse step UI + progress/cancel.
- **T-544** Options step UI: columns, pros/cons, keep/edit rows, re-optimize, infeasible message.

### Phase F — simulation, problems, done
- **T-550** Print simulation step wiring (current cooling/warp/heat → findings + 3D maps; thermal tier A later).
- **T-551** Findings split one-per-instance + short titles + More (engine: `message` keys; `rules.yaml`
  short titles).
- **T-552** Problems step: fixes under each finding, pending design changes with ghost, Confirm all,
  model-change warning, focused re-check (strength floor, checks, quick orientation sanity), New badges.
- **T-553** Done step + optional cards + header Export.

### Phase G — extras
- **T-560** Tolerances 3D (ghost mate first).
- **T-561** Settings & calibration hub (printers, materials owned, calibration pages, active values).
- **T-562** Quality goal path + research task on quality settings (speeds, cooling, ironing, bridges, seams).
- **T-563** "Make it lighter" as optional step in new UI.

### Phase H — wrap up
- **T-570** Remove the old panes/markup/CSS; delete dead code.
- **T-571** e2e smoke rewritten for the new flow (desktop viewport), screenshot review.
- **T-572** Update PRODUCT.md, DESIGN.md, PLAN.md, agents docs (MCP tools for `optimize`).

Existing open tasks superseded by this plan (to close/merge): TASKS.md sections "D. Navigation rebuild",
"K. Phone layout", parts of "C. The first minute", "Levels and steps", "Strength analysis" UI items.
The [TO] thermal tasks continue unchanged and plug into step 9.

---

## 20. Open questions for the user

1. **Settings & calibration entry**: header gear only, or also a third (smaller) entry on the Start screen?
2. **Fast goal safety floor**: you named Strong 5, Balanced 3, Light 1.5. Fast = 2? or 1.5?
3. **Quality goal**: a fifth button in "What matters most?" from day one, or added later?
4. **Force units**: N and MPa only, or also kg (hobbyists think "it holds 5 kg")?
5. **Hold types**: is "this face/edge is held" enough, or do you want "held but can slide/turn" (e.g. a
   pin in a hole) later?
6. **Big nozzles**: from which part size should 0.8/1.0 mm be offered (e.g. longest side > 150 mm or
   volume > 200 cm³)? And only if the printer supports them?
7. **My materials**: should the user list what filament they own, so options start with those?
8. **Tolerances**: which of the four 3D ideas (ghost mate, printed-vs-CAD overlay, pair two parts,
   feeler gauge) first?
9. **Options count**: always 4 columns, or 2–4 depending on how different they are?
10. **Mock review**: do you want to approve static screen mocks before any logic is rewired (my proposal)?
11. **Old features with no place in the flow** (A/B compare, Numbers for CAD, Your versions, Heat inside
    an electronics box, Detailed check from your sliced print, Stronger in one area): keep as optional
    extras on the Done screen, move to Settings, or drop?
