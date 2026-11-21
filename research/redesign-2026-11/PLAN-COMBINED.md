# FDM Check redesign: combined plan

Feedback round 5 (2026-11-21). This plan merges three independent plans:

| Source | File | Strongest contributions |
|---|---|---|
| Claude Opus 5.5 | [plan-claude.md](plan-claude.md) | Flow, screen-by-screen spec, writing rules, component set, task list |
| GPT-6.2 Sol | [plan-gpt.md](plan-gpt.md) | Full-width Options screen, one fix batch that includes slicer changes, revisions and stale results, honest strength wording, optimizer that doesn't miss strong orientations, build one working path first |
| Gemini 4 Neon | [plan-gemini.md](plan-gemini.md) | Pin chips plus constraints bar, inside corners, optional recheck, Pyodide-safe Complete analysis, fast optimizer maths, new visual palette, engine work in parallel with the mockups, homes for existing tools |

Ground truth: [feedback.md](feedback.md) (verbatim). Tags: **[F]** = you said it; **[C] [G] [N]** = proposed by Claude, GPT
or Gemini Neon. The owner's decisions (section 16) override everything else.

---

## 0. Reality check and the decisions that followed

**Owner's decisions of 2026-11-21 are in section 16 and override anything earlier in this file.**

1. **OrcaSlicer in the browser** [F]. WebAssembly builds of OrcaSlicer exist (Muon3D slicer engine, OrcaWasm; see
   `research/browser_slicers.md`), but none is in the app yet. T-127 was postponed on 2026-11-07, waiting for a stable Muon3D
   release (checked v0.6.1: single-thread `engine-st.wasm` 11 MB / 3.4 MB gzip, Orca profiles ~10 MB, AGPL-3.0-only,
   pre-release). **Decision:** bring it in (task T-525). It runs **in the background** (its own worker, loaded lazily) and slices
   the model with the chosen orientation and settings. The G-code feeds the print simulation (Step 9) and the "Download
   G-code" on Done. Until it works, the same slots use our pseudo-slicer, a G-code the user drops in, or local mode.
2. **"The temperature simulation, which we are still working on"** [F]. The detailed heat model and the G-code tuning are
   tasks T-472…T-481, still open, with a go/no-go on real test prints (T-477). Screens are built so these plug in
   later without UI changes.

---

## 1. Global rules

### 1.1 Desktop only [F]
- Delete every phone/tablet rule: all `@media (max-width: …)` blocks, mobile markup, bottom sheets, touch-only paths,
  phone passes in `tools/e2e_smoke.py`. No breakpoints that change layout.
- Supported: **1280 × 720 and up**. Smaller windows or high zoom just scroll; the header and the Next button stay reachable [G].
- `PRODUCT.md` "Operating Context" rewritten: desktop only. The phone promises (reading a report, failed-print path at the printer) are removed.

### 1.2 Header always on top [F]
- App root is a full-viewport grid: `grid-template-rows: 52px minmax(0, 1fr)`. Header explicitly in row 1.
- Toasts, dialogs, menus and anything injected by extensions go into a separate `#portal-root` outside the grid, so nothing
  can push the header down [N]. (Finishes T-491.)
- Header, left → right: brand (click = back to Start, warns about unsaved work) · workflow name ("Optimize your print") ·
  part pill once loaded ("bracket.step · 60 × 30 × 40 mm", click = replace part) · spacer · Undo / Redo · **Export 3MF** ·
  **Settings & calibration** (labelled button with gear) · Help. **No level/mode switch** [F].
- Project menu (Open / Save project, Share report) lives behind the Export split-button / a "Project" menu, never on the drop zone [G].

### 1.3 Levels and Expert [F]
- Remove the Simple/Basic/Advanced/Expert switch and **delete the Expert scroll page**. One flow for now.
- The flow is one declarative list (`web/flow.js`): `{id, label, optional, skipWhen(state)}` [N]. When levels come back,
  **Expert has exactly the same step structure as Advanced**, just more controls inside each step [F].

### 1.4 Writing rules: no walls of text [F]
- Control: **label ≤ 4 words**, optional **hint: one line, ≤ 80 characters**. Nothing between buttons.
- Finding (collapsed): **title ≤ 6 words** + location tag + **one line** of consequence. Everything else behind **More**.
- No brackets in labels ("or saved project" gone [F]).
- One finding per instance: three oval holes = three rows, each pointing at its hole [F]. (The engine already emits one
  finding per hole; the problem was long `detail` text and how the old UI grouped them [N].)
- All text via `t("key")` in `web/messages/en.js` (AGENTS.md). Every string is rewritten, not moved over.
- Rules about tone are guidelines, not a character-count police: the real fix is hierarchy and editing the actual copy [G].

### 1.5 Visual system, new from scratch [F + N + G]
Don't restyle the old CSS and don't keep the old green palette [N].
- **Direction:** calm, neutral desktop workbench (think modern CAD/slicer): neutral cool-grey surfaces, crisp 1 px lines,
  one blue accent for actions, status colours only for status, the 3D part as the hero.
- **Tokens** (`web/tokens.css`): surfaces `#F5F6F8 / #FFFFFF / #ECEFF3`, stage `#E4E8EE→#D8DEE6`, text `#111827 / #4B5563 /
  #6B7280`, accent `#2563EB`; good `#15803D`, warn `#B45309`, bad `#B91C1C`. 3D overlays: hover cyan `#38BDF8`, hold
  blue `#2563EB`, load violet `#9333EA`, added plastic green, removed plastic red [N]. **Light by default with a dark switch** (decision 6); every token has a dark value, and the final look is picked from a few style mockups (T-512).
- **Spacing, only these:** 4 · 8 · 12 · 16 · 24 · 32 · 48 px. Inside a control 8, between controls in a group 12, between
  groups 24, between sections 32. A test (`core/tests/test_css_tokens.py`) fails on any raw px margin/padding/gap [C][N].
- **Type:** Atkinson Hyperlegible; 12 / 13 / 14–15 / 16 / 20 / 28 px; two weights (400, 600); tabular numbers for readouts.
- **Targets:** hit area ≥ 36 × 36 px, primary buttons 42–44 px, visible focus ring on everything [G].
- **Components** (one ES module each in `web/ui/`, no raw browser-styled `<select>`/`<input>`/`<details>` left):
  Button (primary/secondary/ghost/danger) · ChoiceCard · SegmentedControl · Chip · Select (searchable, "Decide later",
  "Owned" badge) · NumberField (unit switch N | MPa, conversion readout) · Disclosure ("More") · RadialMenu (anchored to a
  3D point) · Popover3D · OptionColumn · ConstraintsBar · FindingRow · PendingBar · ProgressCard · Toast · Modal · StepRail.
  Each has default / hover / focus / active / disabled / error states. A gallery page `design/gallery.html` shows them all.

### 1.6 Layouts (exact)
| Screen | Layout |
|---|---|
| Start, Settings | Full canvas, no rail, no 3D. Start centred max 1040 px; Settings `220 px tabs │ content ≤ 880 px` |
| Part (before load) | Rail + big drop zone filling the stage; no right panel |
| Setup … Analyse, Simulation, Problems, Done | `216 px step rail │ 3D stage 1fr │ 384 px right panel` |
| Options | Rail collapses to 56 px icons; **full-width comparison**, 3D preview strip (~220 px) on top [G] |

- Right panel = fixed header (step title + one-line purpose) · scrolling body (padding 20, gap 24) · fixed 64 px footer with
  **Back** (ghost, left) and **Next / Run** (primary, right), **always in the same spot** [F][C].
- 3D stage: small camera bar bottom-left (Iso, Top, Front, Fit, Section, X-ray); one instruction pill top-centre when
  useful ("Hover the part, click where it's held"); legend top-right only when an overlay is on; toasts bottom-centre.
- Step rail: done steps (check, clickable), current (accent), upcoming (muted), skipped steps hidden. Going back keeps
  later inputs; if an upstream input changes, downstream steps show "Needs a re-run" [N][G].

### 1.7 State, revisions, background work [G + N]
- One central store (`web/state.js`) with Undo/Redo (holds, loads, design versions, pending changes) and the existing
  IndexedDB autosave and recent projects.
- Every result carries the revision of its inputs (model, printer/profile, goal, environment, loads, chosen option). When an
  input changes, dependent results are marked **stale**, never silently wrong. Export always names the snapshot it uses.
- Long jobs: one worker, bounded memory, the PC stays usable (existing rule). Always show stage, elapsed, estimate (only
  where measured), **Cancel** (keeps the last valid result) and, for long runs, **Stop & show best so far** [N].

### 1.8 Keyboard [G + N]
Esc closes the topmost menu/popover or cancels the current gesture · Enter confirms a popover · Delete removes the selected
hold/load · Ctrl+Z / Ctrl+Y · with the 3D view focused: F fit, 1/2/3 views, X x-ray. Tab enters the 3D view as one stop;
inside it a searchable feature list lets keyboard users pick faces/edges too. Focus moves to each new step title.

---

## 2. The flow

```
Start ─┬─ Optimize your print ─► 1 Part ─► 2 Setup ─► 3 Environment ─┬─► 4 Holds ─► 5 Loads ─► 6 Load details ─┐
       │                                                              │  (Quality skips 4–6)                   │
       │                                                              └──────────────────────────────┬─────────┘
       │                                                                                             ▼
       │                                                           7 Analyse (Fast / Extended / Complete)
       │                                                           8 Options (columns, pin, re-optimize, pick)
       │                                                           9 Print simulation (cooling, heat, warping)
       │                                                          10 Problems & fixes (pending → Confirm all)
       │                                                          11 Done → Export · optional: Tolerances ·
       │                                                                    Lighter · Tuned settings/G-code · More tools
       ├─ Fix your broken prints (Coming soon)
       └─ Settings & calibration (header button everywhere + slim card on Start)
Export 3MF in the header from step 2 on.
```

---

## 3. Start screen [F]
- Under the header, centred. Heading "What would you like to do?" (one line, nothing else).
- **Two equal big cards** side by side (24 px gap, ~460 × 230 px):
  - **Left: Optimize your print.** One line: "Best orientation, settings and material for your part." Whole card clickable.
  - **Right: Fix your broken prints.** Badge **Coming soon**, plus a short list of what is coming. The card isn't clickable and isn't a tab stop.
- **Below them, a slim full-width card: Settings & calibration** ("Your printers, materials, profiles and test prints") [N].
  You listed settings in the first view near the end of the feedback. The header button is there too. Confirmed (decision 5).
- Recent projects (only if any exist): up to 3 small cards plus "Show all". On a first visit only a quiet "Open a saved project" link.
- The engine warms up silently in the background. The old "What do you want to do?" dialog and the tour are gone.

## 4. Step 1: Your part [F]
- The stage is **one big drop zone** (~600 × 320 px, dashed border that turns accent on drag): icon, **"Drop your part
  here"**, button **Choose file**, small line **"STL, 3MF or STEP"**. Nothing else in or below it, except:
- **Exactly one button under it: Sample parts.** It opens a grid of 6–8 thumbnails with 2–3-word names. A click loads one [F].
- Loading shows progress plus Cancel inside the drop zone.
- Normal load: the part appears and the flow **moves to Setup by itself** [F].
- Special cases, handled before or right after moving on [G][N]:
  - **Several bodies:** they're shown in colours and the user clicks the one to check, then the flow continues.
  - **Unit looks wrong** (2.4 mm wide): an amber bar on the stage asks "Scale from inches (×25.4) or cm (×10)?" (one click, or dismiss).
  - **Damaged mesh:** a toast with More.
  - **Error** (corrupt, too many triangles): the flow stays on Part with a plain reason and Try another file.

## 5. Step 2: Setup ("What you have") [F]
Right panel, three groups:
1. **Printer** (required): searchable Select (presets, the user's printers, "Add another printer…"). Next to it a
   **Manage** button that opens Settings on the Printers tab and comes back here with everything intact [F].
   **Material** and **Nozzle** side by side, both default **"Decide later"** [F] (the optimizer picks and offers
   alternatives). A material or nozzle picked here is a **hard lock**, with a visible ✕ to clear it [G]. Owned materials are listed first.
   **Nozzles to consider** (decision 7): when a printer is picked, a row of nozzle checkboxes appears with every size that printer
   can take (from Settings), **all ticked by default**. Unticking one (e.g. 1.0 mm) means the optimizer never suggests it.
2. **What matters most?** Five ChoiceCards: **Balanced · Strong · Fast · Light · Quality**. Default Balanced. Only the
   selected one shows its one line ("Least plastic, including supports."). [F]
3. **What can you change?** **Slicer settings · The design · Both** (default Slicer settings) [F]. **"The design"**
   (decision 4): the orientation may still change, but strength now comes **mostly from design changes**: hidden sections
   (voids that force solid walls) and thicker areas at weak spots, instead of more walls and infill.
   - **STEP warning:** if "The design" or "Both" is chosen and the part is an STL or 3MF, show one inline warning:
     "Design changes work best with a STEP file. Have one? Load it instead." with a **Load STEP** button. It's a warning
     only, never a block (decision 4: "a warning might be enough for now").
   - Better design edits need a real CAD engine working on STEP (OpenCascade, which the app already loads for STEP import).
     That is a separate research task (T-526); until then the edits use today's mesh path (`manifold-3d`).
- Switching to Quality after loads were entered keeps them in the project (not used). Switching back restores them [G].

## 6. Step 3: Environment [F]
Header line: "Where will it be used? This decides the material."
- **Temperature:** the existing tiers as ChoiceCards with °C (Indoors ~20 · Warm room/electronics ~40 · Outdoors in sun
  ~50 · Hot car/near motor 70+ · Freezing · Custom → one °C field) [F].
- **What will it touch?** Multi-select Chips with icons: Water/wet · Food or drinks · Outdoors (sun & rain) · Dirt &
  scraping · Oil, grease & fuel (workshop) · Cleaners & alcohol · Skin/worn · **None of these** (default) [F list + C/N].
  Unknown stays unknown: "We couldn't verify a material for this use" rather than a guess [G].
- **Live line at the bottom:** "Suitable: PLA, PETG, ASA (+2)". If a material locked in Setup doesn't fit (PLA + hot car),
  an inline card offers **Let the tool choose** [N].
- Food: a short, honest note (layer gaps hold bacteria, the material must be certified, a coating or liner helps), not a promise.
- Data: extend `presets.yaml` materials with `water`, `food_notes`, `abrasion`, `oil_fuel`, `solvent`, `skin` (with sources;
  `heat_limit_c`, `uv_resistance`, `hygroscopic`, `fibre` already exist).
- Next → Holds, or for Quality → Analyse.

## 7. Step 4: Holds ("Where is it held?"), all in 3D [F]
- **On entering:** the engine sends face regions (an id for every triangle, plus area, normal and kind: flat/cylinder/hole/curved),
  feature edges and **inside corners** (`features.concave_edges`) once [N].
- **Hover before click** [F]: a raycast throttled to the frame rate. If the cursor is within ~6 px of an edge or inside corner, that
  line glows. Otherwise the face region under the cursor gets a soft tint and outline, and a small badge says what it is
  ("Ø5 mm hole", "Inside corner"). Nothing is selected by hovering.
- **Click → radial menu at the cursor** [F]: **Hold this face** / **Hold this edge** / **Hold this inside corner** / for a hole
  **Hold this screw hole** · **Cancel**. Kept inside the viewport; Esc closes it.
- Confirmed holds: blue with a lock glyph that stays visible (shows through the part). Clicking a hold opens its menu with **Remove**.
- Hold type is set automatically (screw for holes, clamped for faces) and can be changed in the list row
  (Fixed / Resting / Screw) without cluttering the 3D view [N]. The engine already supports these.
- **Many holds allowed** [F]. At the 4th hold, or > 30 % of the surface: a toast reads "Hold only where it's really fixed. Too many holds hide weak spots." [F]
- Right panel: an empty state with 2 tiny steps (hover to preview, click to hold), then a list of holds (auto-named, e.g. "Bottom
  face · 412 mm²"). Hovering a row pulses it in 3D; ✕ removes it. Footer **Next** after ≥ 1 hold, with "or click more spots" [F].
- **Removed:** "keep this face clean", "put on the bed" and anything about the bed [F].
- Optional later: paint part of a big face with a brush (from the earlier docx feedback).
- **No loads** (decision 3): a clear secondary option on Holds, **"No loads, skip strength"**, skips Holds, Loads, Load
  details and the strength solve.
  - With goal **Fast** or **Light** it just skips.
  - With goal **Strong** or **Balanced** a warning dialog appears first: "Strong and Balanced need loads to work. Without
    loads, switch to: **Fast** · **Light** · **Quality**", plus **Go back and add loads**. Picking a goal changes it in
    Setup and continues to Analyse. (Quality never shows these steps anyway.)

## 8. Step 5: Loads ("Where is it pushed or pulled?"), all in 3D [F]
1. Hover highlight, the same as for holds.
2. **Click → direction gizmo at that point** [F]:
   - One-click arrows: **Push in / Pull out** (face normal) and **±X ±Y ±Z** (axis triad shown).
   - **Rotation rings** like a slicer's rotate tool to tilt the arrow freely. Snaps every 15°, Shift = free. A small
     read-only angle label while dragging. **No angle input boxes** [F].
3. **Force popover at the arrow tip** [F]: a number field, already focused, with the unit switch **N | MPa**.
   - N: a quiet hint "≈ 5.1 kg of weight" [N].
   - MPa: for a face, "3 MPa × 412 mm² = 1 236 N" [F]. Disabled for edges and points ("Pick a face to use MPa").
     Oblique pressure isn't supported at launch: use N [G].
   - Enter / OK sets it; Esc cancels only this draft.
4. The arrow stays on the part (length on a log scale and capped, so small and big loads stay clickable), with a label "50 N".
   Clicking it reopens the gizmo and popover to edit.
- Several loads allowed; by default they all **act together**. A quiet "+ Separate load case" lets advanced users add load
  situations that happen at different times (the worst case counts). Old projects keep their cases [G][N].
- Right panel list: direction icon, place ("Top hook · Pull"), value, edit, ✕. Footer **Next** needs ≥ 1 hold and ≥ 1 load.
- Stored internally in N. Old projects with kg are converted on load [G].

## 9. Step 6: Load details [F]
Three questions, no scrolling:
- **How long does it act?** Short moments · Often, on and off · All the time (creep). "All the time" raises the target (§10.2).
- **Temperature during use:** "We'll check at 50 °C (outdoors in sun). Right?" with **Yes** (default) or **Change** [F].
- **Shaking or knocks?** No / Yes. Yes offers **Light vibration** (optional mounted weight → existing dynamics check) or
  **Hits and drops** (drop height → impact factor) [N]. If the model can't check it: "Shaking and knocks weren't checked;
  try a test print" [G].
- **No print-strength or layer-adhesion inputs** [F].

## 10. Step 7: Analyse ("How deep should we look?") [F]

### 10.1 Screen
- One line summarising printer · goal · what may change, then three ChoiceCards, each with a **time estimate for this part**
  (from triangle count, size, voxel count and candidate count, calibrated on the corpus and measured, not guessed) [F]:
  - **Fast:** strength check, orientation sweep, a few setting combinations.
  - **Extended** (default) [G]: all orientations, walls/top/bottom/infill, layer heights, nozzles, materials that fit the
    environment. No design changes.
  - **Complete:** Extended + **design additions tested automatically at weak spots** (stronger local zones, hidden gaps
    that become solid walls, local thickening/fillets). Can take 10–25 min, and you said that's fine [F].
    Shown but disabled if design changes aren't allowed ("Needs design changes" + **Allow** link). Hidden for Quality.
- **Start optimization**. While running: a stage list with ticks (Finding orientations · Checking strength · Measuring
  support · Trying nozzles, layers and settings · Testing local reinforcements · Picking options), elapsed, estimate,
  **Cancel**, and **Stop & show best so far** once a valid option exists [N]. If the time budget runs out: "Search ended
  early", plus Continue. Running out never means "impossible" [G].

### 10.2 Safety targets [F]
| Goal | Target (floor) | Then optimise for |
|---|---|---|
| Strong | ×5 | Highest safety; support plastic hardly matters [F] |
| Balanced | ×3 | Time + total plastic (part + support) + some extra safety; support matters a bit [F] |
| Fast | ×2 | Shortest print time; bigger nozzles and thicker layers allowed [F] |
| Light | ×1.5 | Least total plastic **including support**, which matters most here [F] |
| Quality | none | No strength at all (§14) [F] |

- A target is a **floor**: candidates below it are dropped from normal ranking.
- Load acts all the time: target = the higher of the goal floor and the existing sustained-load floor (×3) [G].
- Wording is honest: "Estimated safety ×3.4 · target ×3", never "it will never break" [G]. If nothing reaches the floor:
  "None of the options we checked reached ×3. Best: ×2.1", plus one-click ways out: **Allow PETG/ASA** · **Allow design
  changes** · **Use ×2.1 anyway** (allowed after a clear warning, which is carried to the export; decision 2) [C][G][N].

### 10.3 How the optimizer works (engine)
New Pyodide-safe module `core/fdmcore/optimizer.py`, `api.optimize(...)`, **with its MCP tool in the same PR** (a test
fails otherwise) [N], plus `make_manifest.py`. It streams progress and partial results.

1. **Orientations.** Start from the full candidate set (`orientation.candidate_directions` + resting planes) and the
   as-loaded pose. **Keep separate shortlists** for strongest, least support, fastest and easiest to print, so a strong pose isn't
   thrown out by a printability prefilter before strength is scored. Today's API does exactly that and can miss it [G].
   One isotropic solve scores all directions (~3–5 s) [N]. Mirror twins are merged only if they're truly equivalent.
2. **Support grams.** For the ~6–10 finalists, sliced `support_stats`, cached per overhang angle (only 3 distinct values
   across the built-in materials) [N]. Part grams, support grams and total are shown separately. If unknown, it's marked
   unknown, never counted as zero [G].
3. **Materials.** Filtered by environment, printer (enclosure, hardened nozzle) and locks. Stress doesn't change with
   uniform stiffness, so all materials for one pose are compared with **arithmetic, no extra solves** [N].
4. **Nozzle and layer height** [F]. Only the nozzles ticked in Setup (default: every size the printer can take). 0.2 only
   for fine detail or Quality. **Rule for anything bigger than 0.4 (decision 7):** there's no size cut-off. Compare the
   bigger nozzle with 0.4 on this part. Suggest it only if the print still works (no small features lost: thin walls, small
   holes, gaps, text, checked with the print checks at that line width) **and** the gain in time or strength is
   significant (proposed: ≥ 15 % time or a clear safety gain, tuned on the corpus). Otherwise drop it and say so in More
   ("1.0 mm skipped: the 2 mm slot would close"). Add a
   `1.0` nozzle preset and let `Profile` take a layer-height override [N]. **Thicker layers aren't simply stronger**: bonding
   rises up to about 0.5–0.6 × nozzle and falls above about 0.7 ×. Use a sourced, capped `layer_bond_factor`. No strength
   bonus without evidence [G][N].
5. **Walls, top/bottom, infill.** A greedy or beam search around promising candidates, with warm-started solves (~1 s per step) [N].
   Lightning infill is excluded when there are loads.
6. **Verify the finalists** with the full anisotropic check at their real pose and settings, plus print checks. Only verified
   candidates can be shown as "meets target" [G].
7. **Complete only** [N + G]: take the top stress clusters on the best 2 poses and test, **in voxel space inside Python**:
   (a) modifier zones (more walls/infill locally → exported as 3MF modifiers), (b) hidden gaps (voids that force solid
   inner walls), (c) local thickening/fillets. Python never calls mesh booleans (`booleans.py` is local-only). Only the
   winning options get real edit meshes, built in the browser with `manifold-3d`, then re-checked (watertight, loads still
   map, slice shows the intended walls) before any improvement is claimed [G].
   **"The design" only (decision 4):** the recipe (walls, infill, layer height, nozzle, material) stays at the user's
   baseline. The search covers orientation + design additions (hidden sections, thicker areas, fillets) at every depth,
   not only Complete. Fast/Extended try the cheap voxel candidates; Complete tries more and bigger ones.
8. **Pick 2–4 options** from the Pareto set that really differ (≥ 10 % time or ≥ 15 % grams or a different material, nozzle
   or reinforcement). Typical: **Best for your goal** · **Least support** · **Uses a material you have / PLA** ·
   **Fastest / big nozzle** or **Reinforced** (Complete). Never padded to four [G][N].

## 11. Step 8: Options [F]
- **Layout:** rail collapses, comparison uses the full width. On top, a ~220 px 3D strip shows the focused column's part on
  the bed (support preview; toggles Print view / Stress / Reinforcements). Below it, **2–4 columns ≥ 200 px** with aligned
  rows and sticky row labels [G].
- **Column top:** name badge (Recommended, Least support, PLA only, Fastest), then **Pros** (≤ 3, green) and **Cons** (≤ 3,
  amber), from checked numbers ("28 min slower", "Needs a 0.6 nozzle") [F].
- **Rows** (the same in every column): Orientation (small bed icon + "Flat on back"; hovering rotates the 3D preview) ·
  Material (Owned / Not on your shelf) · Nozzle · Layer height · Walls · Top/bottom · Infill · Support grams · Total grams
  (+ cost if a price is set) · Print time · Safety (pill: "Enough · ×3.2") · Reinforcements (Complete only).
- **Keeping parts of an option** [F], without a wall of checkboxes [N]:
  - Hovering a value shows a **pin**. Clicking a value lets you edit it inline.
  - Pinned or edited values become chips in one **"Your choices" bar** under the columns: `[Material: PLA ×] [Nozzle: 0.4 ×]`.
    You can mix them (material from one column, orientation from another). If two pins conflict, it asks **Replace** or **Cancel** [G].
  - **Keep all** on a column header pins its whole recipe [C][G].
  - **Re-optimize with my choices** reruns with the chips as hard limits (reusing what didn't change) and shows new columns that all
    respect them [F], or the honest infeasible message with the ways out (§10.2) [F].
  - Results (time, safety) can't be pinned. Edited columns show "Needs a re-check" and can't be used until rerun [G].
- **Use this option** under each column → applies it and goes to Simulation. Back returns to the exact same comparison.

## 12. Step 9: Print simulation [F]
- Panel: one strip with the chosen setup ("Flat · PETG · 0.4 · 3 walls · 15 %", **Change** → Options). Print conditions are pre-filled
  from the printer and material in one collapsed group (enclosure, bed temperature, room temperature), with the source of each
  value shown [G][N].
- **Real G-code in the background (decision 1):** as soon as an option is chosen, the browser OrcaSlicer (T-525) slices it in its
  own worker with the chosen orientation and settings, while the user looks at this step. The G-code goes to the
  existing G-code reader, spot heat and G-code-timed warping, and later to the thermal model. Progress shows as one line
  ("Slicing with OrcaSlicer… 40 %"). If the slicer isn't available (still loading, failed, or too big), fall back to our
  pseudo-slicer and say so. Local mode uses the installed OrcaSlicer. A G-code the user drops in ("Have a sliced
  file?") still works [G][N].
- **Run simulations.** Day 1: print checks + `heat.py` + `warp.py` (+ spot heat with G-code). Later, in the same button: the
  thermal model (T-473…T-476), with delamination and bonding fed back into safety ("Root ×3.2 → ×2.8 after cooling").
- Results stay on this step first: a 3-item status (Cooling · Warping · Print issues: 4) and an overlay switch on the stage
  (Normal · Heat · Warping (actual / exaggerated, labelled) · Internal stress · Overhangs). Footer: **Review problems (4) →**
  or **All clear → Done**. Skipping is allowed; skipped checks are named in Problems and in the export [G].

## 13. Step 10: Problems & fixes [F]
- List grouped by impact (Can fail · Shape off · Looks rough · Weak · Info). Empty groups are hidden; Info is collapsed when there
  are worse problems.
- **Row, collapsed (~52 px):** dot · **short title** ("Side hole prints oval") · location pill ("Ø6 · side") · one line
  ("Top sags ~0.3 mm; a 6 mm bolt won't fit") · status (Pending / Fixed / Accepted / **New**). Click → the camera frames
  that one feature [F].
- **Row, expanded:** **Fixes come first, directly under the problem** [F]. Each fix shows a tag (Slicer / Design / Process), one line
  (with the CAD number if relevant: "Thicken to 1.35 mm, now 1.0") and **Add** (or **Show steps** for process tips). Then
  **More** with the explanation, numbers and sources. A design-only problem still shows when design changes aren't allowed [F]; with no
  automatic fix it says what to do in CAD [N]. **Accept this issue** is a quiet link.
- **Design fixes when the user said "Slicer settings only"** [F]: still shown, tagged "Changes your model". Add opens a
  one-time confirm: "This changes your model. You can undo it." The same rule applies in Tolerances [G].
- **Nothing recomputes while picking** [F]. Every added fix (design **and** slicer, since walls, supports or fans also change
  strength and cooling [G]) goes into **one pending batch**: design changes show as ghosts (green added, red removed),
  setting changes show old → new. The sticky bar reads **"3 changes pending · Review · Discard · Confirm all changes"**.
  Conflicting fixes (same hole, same setting) must be resolved first [G].
- **Confirm all changes:**
  1. Builds a candidate from the last committed version and applies the edits one by one with the existing safety checks
     (watertight, volume, pieces). A broken edit is skipped with a plain message and its CAD number [N].
  2. **Quick re-check (default):** strength at the chosen orientation and settings (is it still ≥ the target, close to before?), the print
     checks and the simulations the change affects. A quick background orientation check only *suggests* ("Another
     orientation is now much better. View options?"), and never turns the part. No full re-optimization [F].
  3. Option **"Apply without re-checking"**: results are marked stale until re-run [F "or we ask if we should recheck"][N][G].
  4. New problems → back to the list with the banner "Your changes caused 2 new problems" and **New** badges, plus
     **Undo batch**. A drop below the target needs an explicit OK [F][G]. Failure or cancel → the last valid version stays [G].
- **Continue to Done** (asks to confirm or discard pending changes first). With nothing left: "No problems found in the checks that ran", naming any skipped checks.

## 14. Quality goal [F]
- Flow: Part → Setup → Environment → Analyse (Fast / Extended) → Options → Simulation → Problems → Done.
  **No holds, loads, load details or strength solve anywhere** [F]. Environment still applies [F].
- Start from **2 walls, 10 % infill** [F]. More only where a top or wall needs it to print.
- Ranking: reliable print first, then least support **and support away from visible faces**, surface (stair steps on shallow
  slopes, small detail, seam placement), warping, delamination and uneven temperature, then time [F].
- Visible faces: start by assuming the outer exposed faces; optionally mark a show face in 3D Guess first (decision 10).
- Problems: printability and looks only (round off this edge, help this overhang), no structural ribs [F].
- Done extras: Tolerances yes (fits matter) [F]; **Lighter no, design additions no** [F].
- Research task: outer wall speed and acceleration, cooling, bridge speed and fan, ironing, seams, wall order → sourced
  `rules.yaml` / thresholds + new calibration tests (ironing, temperature tower) [F].
- Quality ships **with the first working flow**, not at the very end: its optimizer needs no strength solve [G]. The
  research makes it better later.

## 15. Done, extras, Settings, Fix prints

### 15.1 Step 11: Done [F]
- "Your part is ready." Summary grid: material, nozzle/layer, walls/infill, time, plastic (+ support), safety (+ cost).
  "Slicer changes (6)" disclosure: difference from the user's imported profile, with a tick per row for export.
- **Export 3MF** (primary; target slicer remembered: OrcaSlicer / Bambu Studio / PrusaSlicer). Modified STL/STEP if the
  shape changed (if STEP can't hold an edit, offer STL and say why). Save project · Share report · Copy CAD checklist.
- **Export 3MF in the header at any time** [F]: a small summary says exactly what goes out ("Current part · no optimized
  settings yet", or the last confirmed snapshot if changes are pending or stale) [G].
- **Optional extras:**
  - **Tolerances & fits** (§15.2), for all goals.
  - **Make it lighter** ("Want it even lighter?") [F]: the goal's target stays locked; shells/infill/modifiers, then shape;
    standard comparison + confirm. Hidden for Quality [F].
  - **Tuned settings & G-code** [F]: today, height-range fan/speed/layer-time changes exported in the 3MF (T-479). **Download
    G-code** from the browser OrcaSlicer (T-525) with the chosen settings, or from local mode. Thermal-tuned G-code (the
    rewrite) only after the T-477 go [G][N]. Temperature
    changes go into the recipe for a fresh slice.
  - **More tools** (collapsed) [N]: manual zones and hidden gaps (the old "advanced design changes"), split part, copies on the plate,
    electronics-box heat, versions and A/B compare (each tagged "your slicer can do this too" where true) (decision 9).
  - Split part and pause-to-insert also appear as fixes under their findings in Problems [N].

### 15.2 Tolerances & fits, in 3D [F asked for ideas]
1. **Click a hole/peg → ghost mate** (build first). Hover shows holes, pegs and slots with "Ø5 mm". Clicking one slides a
   translucent pin (or socket) in with an animation, plus a coloured **clearance ring** (red: will jam on your printer; amber: tight;
   green: fits) labelled "0.18 mm gap (measured on your P1S)" or "(typical PLA)". The popover has **Press · Snug · Loose**,
   the mating size (prefilled), and a Screw/insert tab (M2–M6 guessed from Ø; screw into plastic, heat-set insert,
   through bolt, nut pocket) with a ghost screw or insert. "Apply to all 4 × Ø3 holes" is on by default. A section view
   is available, and the motion respects reduced motion [C][G][N].
2. **Printed vs CAD overlay** (second): an orange outline of what the printer will actually make, ×5 / ×10 exaggerated, using
   the calibration values (hole error, shrink, elephant foot).
3. **Mating part heat map** (third): load the second part, paint clearance on the contact faces, mark the tightest spot.
4. **Feeler gauge** for flat gaps and print-in-place (fourth): a blade slides through and turns red where it jams.
- Output: hole compensation as a slicer setting if only settings may change (it affects **every** hole, so all affected
  holes are shown), otherwise resized geometry as pending design changes (same confirm path) [G].

### 15.3 Settings & calibration hub [F]
Opened from the header button, the Start card and Setup's **Manage**. **Back to your part** keeps all work. Built
**early** (alongside Setup), so Manage works from day one [G][N].
- **Printers:** add from presets / the 415+ OrcaSlicer catalogue / "My printer isn't listed" helper. Per printer: bed, enclosure,
  hardened nozzle, bed slinger, plate surface, **nozzles available** (0.2–1.0). Source labels: Built-in, Imported,
  Measured.
- **Print profiles:** drop an Orca/Bambu `.json` or Prusa `.ini` → preview recognised, missing and conflicting values →
  save as a baseline (speeds, walls, ironing, retraction…) [F]. Locks chosen in a project beat profile values; measured values
  beat imported ones, but never exceed machine limits [G].
- **Materials:** "Filaments I own" checklist + custom filaments + import. Optional price/kg and grams left. Options then prefer
  owned filaments, plus an explicit "Only my materials" constraint [G][N].
- **Calibration tests:** cards with Measured / Using typical: starter plate, fits/tolerance, overhang, bridge, warping
  bar, layer strength, + ironing and temperature tower (Quality), + sag & snap tests (T-482). Each opens a page: one line on what
  it improves · **Download model** · 3–5 numbered steps with a picture · **Enter result** with a visual picker (click the best
  segment or hole) → saved in the browser [F].
- **What the checks use now:** a table of active values (Measured by you / Typical) with Reset, plus **Export/Import all my
  settings (JSON)** so clearing browser data doesn't lose calibrations [N].

### 15.4 Fix your broken prints [F]
Only the Coming-soon card. The existing "My print failed" pane is hidden and kept as seed material for that future plan.

---

## 16. Owner's decisions (2026-11-21), these override the rest of this file

1. **Browser slicer:** yes. Use a browser build of OrcaSlicer **in the background** to get G-code for the model with our chosen
   orientation and settings (T-525, reviving T-127). It feeds Simulation and the G-code download. Fallbacks as in §12.
2. **Safety targets:** Strong ×5, Balanced ×3, Fast ×2, Light ×1.5. An option below the target **can be exported after a
   clear warning**, and the warning travels with the export.
3. **No loads:** "No loads, skip strength" is an option on Holds and skips the strength steps. With **Strong** or **Balanced** it
   first shows a warning asking to switch to **Fast, Quality or Light** (§7).
4. **"The design" only:** orientation may still change. Strength then comes **mostly from design changes** (hidden sections,
   thicker areas). That needs a better geometry engine, a real CAD engine working on **STEP** files (research task
   T-526). For now, a **warning** suggests loading a STEP instead of an STL when design changes are chosen (§5).
5. **Start screen:** two big cards + slim Settings & calibration card + header button. Yes.
6. **Theme:** **light, with a dark switch.** Do a few style mockups (different looks, each light and dark) so the owner can pick
   the final design.
7. **Nozzles:** when a printer is picked, show its nozzle sizes as checkboxes, **all ticked by default**. The user can untick
   sizes they don't want suggested (e.g. 1.0). A nozzle bigger than 0.4 is suggested only if the print still works with it
   (no small features failing) **and** the difference from 0.4 is significant. No fixed part-size cut-off (§10.3).
8. **Tolerances order:** ghost mate → printed-vs-CAD overlay → mating part → feeler gauge. Yes.
9. **Old tools:** under **More tools** on Done (split part and pause-to-insert also as fixes under their problems). Yes.
10. **Quality, visible faces:** guess first (outer exposed faces); the user can mark a show face if needed.
11. **Review gate:** **static style mockups only, not clickable.** The owner approves them before the new shell is wired.

---

## 17. Implementation plan

The tasks are in [TASKS.md](../../TASKS.md), section **"UI revision (2026-11-21, tag [UI])"**, T-500…T-582. Each task
cites the sections of this file it implements. Ground rules for all of them:

- One branch per task from `main` (AGENTS.md), PRs against `main`, tests locally before pushing.
- Engine work runs in parallel with the style mockups [N]. The new UI is built in clean modules from the first screen
  (`web/ui/`, `web/steps/`, `web/viewport/`, `web/state.js`, `web/flow.js`), behind one cut-over of `index.html`. The old
  `app.js` isn't refactored, it's replaced [N].
- First goal: **one complete working path** (sample → Setup → Environment → Holds/Loads → Analyse → Options → Problems →
  Export) before polishing the extras [G].
- The thermal tasks T-472…T-484 continue unchanged and plug into Simulation and Done.
- Every task: affected pytest files while working; full pytest + e2e before marking done if `web/` or `api.py` changed;
  `node tools/pyodide_check.mjs` after engine geometry changes; `python tools/slicer_check.py` after 3MF/modifier changes;
  `python tools/make_manifest.py` after adding engine files.

---

## 18. Where the three plans disagreed and what this plan picked

| Topic | Claude | GPT | Gemini | Picked |
|---|---|---|---|---|
| Settings on Start | small link | header button only | third card + header | slim third card + header (decided) |
| Options layout | inside the right panel | full width + 3D strip | 38 % 3D / 62 % columns | **full width + 3D strip** (4 columns fit at 1280) |
| Keeping values | a checkbox per row per column | keep per cell + summary | hover pin + chips bar | **pin + "Your choices" bar + Keep all** |
| Slicer fixes | applied instantly | in the pending batch | applied instantly | **in the batch** (they change strength/cooling) |
| Re-check after confirm | always | always, keep last valid | optional toggle | **quick re-check by default, "apply without" option** |
| No-load parts | Quality only | not covered | skip link | "No loads" option; Strong/Balanced warn and offer Fast/Quality/Light (decided) |
| Hold types | face/edge | face/edge, keep imported | auto type + list toggle | **auto type, change in the list** |
| Units | N, MPa (kg asked) | N, MPa | N, MPa + ≈ kg hint | **N, MPa + read-only ≈ kg** |
| Thicker layers | stronger | no general rule | sourced curve | **sourced capped curve** |
| Complete depth | additions/placement | modifiers first, validate | voxel-space in Python | **voxel space, meshes only for winners, validated** |
| Creep | not covered | max(goal, ×3) | per-goal table | **max(goal floor, ×3)** |
| Palette | keep old tokens | neutral + blue | new neutral + blue | **new neutral + blue, light with dark switch** (decided) |
| Order | clean-up 3 PRs, Quality late | working path first, Quality early | triage 1 PR, engine parallel, Settings early | **1-PR triage, engine parallel, Settings early, working path first, Quality early** |
| Mockups | static, gate | interactive prototype | static, gate | **static style mockups only** (decided) |
