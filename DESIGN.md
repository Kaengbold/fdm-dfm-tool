---
name: FDM Check
description: A desktop browser app that finds the best way to print one part, checks it, fixes it and exports it for the slicer.
colors:
  surface: "#F5F6F8"
  surface-raised: "#FFFFFF"
  surface-sunken: "#ECEFF3"
  line: "#D9DEE5"
  line-strong: "#B8C0CC"
  stage-top: "#E4E8EE"
  stage-bottom: "#D8DEE6"
  text: "#111827"
  text-2: "#4B5563"
  text-3: "#606774"
  text-on-accent: "#FFFFFF"
  text-on-load: "#FFFFFF"
  accent: "#2563EB"
  accent-hover: "#1D4ED8"
  accent-soft: "#DBE6FD"
  accent-text: "#1D4ED8"
  good: "#147B3B"
  good-soft: "#DCF3E4"
  warn: "#AE5009"
  warn-soft: "#FCEBD5"
  bad: "#B91C1C"
  bad-soft: "#FBE0E0"
  ov-hover: "#38BDF8"
  ov-hold: "#2563EB"
  ov-load: "#9333EA"
  ov-added: "#16A34A"
  ov-removed: "#DC2626"
typography:
  start:
    fontFamily: "Atkinson Hyperlegible, system-ui, sans-serif"
    fontSize: "28px"
    fontWeight: 700
    lineHeight: 1.2
  step-title:
    fontFamily: "Atkinson Hyperlegible, system-ui, sans-serif"
    fontSize: "20px"
    fontWeight: 700
    lineHeight: 1.2
  title:
    fontFamily: "Atkinson Hyperlegible, system-ui, sans-serif"
    fontSize: "16px"
    fontWeight: 700
  body:
    fontFamily: "Atkinson Hyperlegible, system-ui, sans-serif"
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.45
  hint:
    fontFamily: "Atkinson Hyperlegible, system-ui, sans-serif"
    fontSize: "13px"
    fontWeight: 400
  badge:
    fontFamily: "Atkinson Hyperlegible, system-ui, sans-serif"
    fontSize: "12px"
    fontWeight: 700
rounded:
  sm: "4px"
  r: "8px"
  lg: "12px"
  pill: "999px"
spacing:
  s-1: "4px"
  s-2: "8px"
  s-3: "12px"
  s-4: "16px"
  s-5: "24px"
  s-6: "32px"
  s-7: "48px"
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.text-on-accent}"
    rounded: "{rounded.r}"
    height: "44px"
    padding: "0 24px"
  button-secondary:
    backgroundColor: "{colors.surface-raised}"
    textColor: "{colors.text}"
    rounded: "{rounded.r}"
    height: "36px"
    padding: "0 16px"
  button-ghost:
    backgroundColor: "transparent"
    textColor: "{colors.text-2}"
    rounded: "{rounded.r}"
    height: "36px"
  card:
    backgroundColor: "{colors.surface-raised}"
    textColor: "{colors.text}"
    rounded: "{rounded.r}"
  card-selected:
    backgroundColor: "{colors.accent-soft}"
    textColor: "{colors.text}"
    rounded: "{rounded.r}"
  tag-slicer:
    backgroundColor: "{colors.accent-soft}"
    textColor: "{colors.accent-text}"
    typography: "{typography.badge}"
    rounded: "{rounded.sm}"
  tag-design:
    backgroundColor: "{colors.warn-soft}"
    textColor: "{colors.warn}"
    typography: "{typography.badge}"
    rounded: "{rounded.sm}"
---

# Design System: FDM Check

The source of truth is `web/tokens.css`: every colour, space, size and radius the stylesheets use comes from there
(`core/tests/test_css_tokens.py` fails on raw pixel spacing anywhere else). This file says what the tokens are for.
The plan behind it is [research/redesign-2026-11/PLAN-COMBINED.md](research/redesign-2026-11/PLAN-COMBINED.md) (§1, Style A,
approved by the owner on 2026-11-21).

## Overview

**Creative North Star: "The part first"**

The part is the biggest thing on screen, in 3D, from the first step to the export. Around it: a header that is always
on top, a step rail on the left that reads like a checklist, and a panel on the right with only what the current step
needs, its one primary action always in the same place at the panel's foot. Picking happens on the part (holds, loads,
show faces, problems), not in lists.

The look is light, cool and quiet: near-white surfaces with a faint blue-grey cast, dark ink, hairline borders, 8 px
corners. One blue accent means "act here". Green, amber and red mean status only. A dark theme switches every colour
token (`<html data-theme="dark">`, the moon button in the header, remembered in the browser).

**Key characteristics:**
- Desktop only: windows of 1280 × 720 and larger. No phone layouts.
- Header on top; rail │ 3D stage │ panel below it.
- One primary button per step, bottom right of the panel ("Next", or the step's own next move: "Review 3 problems").
- Text is short. One sentence per line of hint; explanations behind **More**.
- Background work shows on the stage (a progress card with stages, elapsed time and Cancel), never only in the panel.

## Colours

- **Surfaces.** `surface` is the app background, `surface-raised` the panels, cards and inputs, `surface-sunken`
  wells (rows inside a card, segment tracks). `line` for dividers and control borders, `line-strong` when hovered.
- **The stage** is a soft vertical gradient (`stage-top` → `stage-bottom`). The part and the bed are drawn in greys
  from the stage palette (`viewport/stage.js`, light and dark).
- **Ink.** `text` for content, `text-2` for secondary lines, `text-3` for hints and units.
- **Accent** (blue) is for actions and selection only: primary buttons, links (`accent-text`), the selected card or
  step (`accent-soft`), focus. Never decoration, never a heading colour.
- **Status.** `good`, `warn`, `bad`, each with a `-soft` background, for verdicts, badges, the strength target and
  problem dots. Text in the strong colour on the soft fill.
- **3D overlays** (`ov-*`) are read by the viewport from CSS, so the part and its legend never disagree: hover sky
  blue, holds blue, loads violet, added plastic green, removed plastic red. Result fields (heat, warping, stress) use
  one banded blue-to-red scale (`fieldColor`).

**Rule: status colours mean status.** A warning is amber because something needs a look, never because amber looks nice.

## Typography

One face, Atkinson Hyperlegible, self-hosted in 400 and 700 (the font has no 600). Hierarchy comes from weight and a
short ramp: 28 px start screen, 20 px step title, 16 px panel and card titles, 15 px body and controls, 13 px hints,
12 px badges and units. Numbers that change (times, grams, safety, sizes) use tabular figures (`.num`).

Sentence case everywhere. No uppercase labels, no letter-spacing.

## Layout

The app is one grid: a 56 px header, then the screen. Screens have one of four layouts (`data-layout`):

- **full** (Start, Settings & calibration): one centred column, no rail, no 3D.
- **part** (before a file is loaded): rail and a stage that is all drop zone.
- **work** (Setup … Analyse, Simulation, Problems, Done): rail 216 px │ stage │ panel 384 px. The panel scrolls inside;
  its 64 px foot holds Back (left) and Next (right) in the same place on every step.
- **options**: the rail collapses to 56 px icons, the 3D stage takes about 40% of the remaining width, and the
  comparison takes about 60%. The footer spans the stage and comparison.

Spacing comes only from `--s-1` … `--s-7` (4 to 48 px): 8 inside a control, 12 between controls, 24 between groups,
32 between sections. Stage furniture: camera bar bottom-left, instruction pill top-centre, legend top-right, toasts
bottom-centre, all 16 px from the edges.

## Elevation and shapes

Flat by default: zones are separated by tone and 1 px lines. `shadow-pop` is for things that float: menus, pop-overs
on the part, the progress card, toasts, dialogs. Corners: 8 px on buttons, inputs and cards; 4 px on tags; 12 px on
dialogs and the progress card; pills for chips and the part name in the header.

## Components (`web/ui/`)

- **Button** (`button.js`): primary (accent fill, 44 px), secondary (raised, hairline), ghost (no fill), small (32 px)
  and icon-only (36 px, with a title). Disabled buttons say why in their title.
- **ChoiceCards / SegmentedControl** (`choice.js`): radio groups as cards (goal, depth, with a one-line explanation
  and an estimate) or as a segmented track (units, overlays, the export slicer). The selected card takes
  `accent-soft` and an accent border.
- **Select** (`select.js`): a searchable list with optional "Decide later" and badges ("Owned").
- **NumberField** (`numberfield.js`): a value with its unit (N / MPa, mm, °C) and a live hint ("≈ 5.1 kg of weight").
- **Chip** (`chip.js`): toggle chips (nozzle sizes) and removable chips ("Your choices" in Options).
- **Disclosure** (`disclosure.js`): a closed section with a summary on its head ("Slicer changes (12)", "More tools").
- **OptionColumn** (`options.js`): one option per column, rows aligned across columns, a pin on hover to keep a value.
- **FindingRow / PendingBar** (`finding.js`): a problem in one short row (dot, title, place pill, status) that opens
  to its fixes (Slicer / Design / Process tags, Add or Show steps), then More; the sticky bar of the pending batch.
- **ProgressCard** (`progress.js`): named stages, elapsed vs estimate, Cancel. Lives on the stage.
- **Overlays** (`overlay.js`, `popover3d.js`, `radialmenu.js`): dialogs and toasts; a pop-over anchored to a point
  on the part (the load's force box); the radial menu at a picked spot ("Hold this face").
- **StepRail** (`steprail.js`): done steps are links with a green dot, the current one is highlighted, upcoming ones
  plain text, skipped ones left out; a done step whose result is out of date says "Needs a re-run".

In the 3D view (`web/viewport/`): the stage with its camera bar (Iso, Top, Front, Fit), the hover picker with a badge
("Flat face · 1,622 mm²"), the direction gizmo for loads, result fields with true-size or exaggerated warping, and
see-through ghosts for pending design changes.

## Do and don't

**Do**
- Keep the part in view; a new feature is picked on the part where it can be.
- Give each step one primary action at the bottom right of the panel.
- Say what a change does to results ("These results are from before your last changes", "Needs a re-run").
- Take every value from `tokens.css`, and every 3D colour from a CSS custom property.
- Check both themes.

**Don't**
- Add phone or tablet layouts.
- Use the accent or a status colour as decoration.
- Put shadows on cards, buttons or inputs inside the panel.
- Write more than a sentence where a hint is enough; put the rest behind More.
- Show background work only in the panel.
