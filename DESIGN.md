---
name: FDM Check
description: A browser workbench that checks one 3D-printed part before it goes to the slicer.
colors:
  accent: "#1f5c47"
  accent-hover: "#174a39"
  accent-soft: "#e2efe8"
  deep-green: "#173a2e"
  now: "#f2c12e"
  text: "#1d2b27"
  muted: "#56645f"
  panel: "#fdfdfc"
  chrome: "#f3f5f2"
  soft: "#eef2ef"
  stage: "#e9ecea"
  line: "#dbe2dd"
  line-strong: "#c3cdc6"
  good: "#22784a"
  good-soft: "#e1f2e8"
  warn: "#8f5600"
  warn-soft: "#fbf0d9"
  bad: "#b82d35"
  bad-soft: "#fbe5e6"
  part-ok: "#a3acb4"
  part-borderline: "#f0b429"
  part-support: "#e5484d"
  part-bed: "#30a46c"
  part-fixed: "#0090ff"
  part-force: "#8e4ec6"
  part-clean: "#12a594"
  part-vertical: "#f76b15"
typography:
  headline:
    fontFamily: "Atkinson Hyperlegible, system-ui, sans-serif"
    fontSize: "23px"
    fontWeight: 700
    lineHeight: 1.2
  brand:
    fontFamily: "Atkinson Hyperlegible, system-ui, sans-serif"
    fontSize: "18px"
    fontWeight: 700
  title:
    fontFamily: "Atkinson Hyperlegible, system-ui, sans-serif"
    fontSize: "15px"
    fontWeight: 700
    lineHeight: 1.45
  body:
    fontFamily: "Atkinson Hyperlegible, system-ui, sans-serif"
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.45
  control:
    fontFamily: "Atkinson Hyperlegible, system-ui, sans-serif"
    fontSize: "14px"
    fontWeight: 400
  label:
    fontFamily: "Atkinson Hyperlegible, system-ui, sans-serif"
    fontSize: "14px"
    fontWeight: 700
  hint:
    fontFamily: "Atkinson Hyperlegible, system-ui, sans-serif"
    fontSize: "13px"
    fontWeight: 400
  badge:
    fontFamily: "Atkinson Hyperlegible, system-ui, sans-serif"
    fontSize: "12px"
    fontWeight: 700
rounded:
  swatch: "2px"
  r: "6px"
  track: "9px"
  overlay: "10px"
  dialog: "12px"
  pill: "999px"
spacing:
  xs: "4px"
  sm: "6px"
  md: "10px"
  lg: "14px"
  xl: "24px"
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.panel}"
    typography: "{typography.label}"
    rounded: "{rounded.r}"
    padding: "9px 16px"
  button-primary-hover:
    backgroundColor: "{colors.accent-hover}"
  button-primary-big:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.panel}"
    rounded: "{rounded.r}"
    padding: "13px"
    width: "100%"
  button-secondary:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.text}"
    typography: "{typography.control}"
    rounded: "{rounded.r}"
    padding: "6px 12px"
  button-ghost:
    backgroundColor: "transparent"
    textColor: "{colors.muted}"
    rounded: "{rounded.r}"
    padding: "6px 12px"
  segment-selected:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.panel}"
    typography: "{typography.label}"
    rounded: "{rounded.r}"
    padding: "5px 12px"
  input:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.text}"
    typography: "{typography.control}"
    rounded: "{rounded.r}"
    padding: "5px 7px"
  step-now:
    backgroundColor: "{colors.accent-soft}"
    textColor: "{colors.text}"
    rounded: "{rounded.r}"
    padding: "8px 10px"
  step-dot-now:
    backgroundColor: "{colors.now}"
    textColor: "{colors.text}"
    size: "26px"
    rounded: "{rounded.pill}"
  chip-slicer:
    backgroundColor: "{colors.accent-soft}"
    textColor: "{colors.accent}"
    typography: "{typography.badge}"
    rounded: "{rounded.pill}"
    padding: "1px 8px"
  finding-card:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.text}"
    rounded: "{rounded.r}"
    padding: "9px 11px"
  working-bar:
    backgroundColor: "{colors.deep-green}"
    textColor: "{colors.chrome}"
    rounded: "{rounded.overlay}"
    padding: "11px 14px"
---

# Design System: FDM Check

## Overview

**Creative North Star: "The Workbench"**

FDM Check is a bench, not a form. Three fixed zones sit side by side at full viewport height: a pale step rail that reads like a checklist, the part itself large in the middle on a calm grey stage, and a near-white panel on the right holding only the current step's controls, with one primary action pinned at its foot. Nothing scrolls the whole page on desktop; the part never leaves view.

The world is light and quiet: near-white surfaces with a faint green cast, dark-green ink and actions, hairline borders, small 6px corners, no gradients. One loud colour exists, a signal yellow, and it means exactly two things: "you are here" (the current step's dot) and keyboard focus. Every other colour is either green ink or a meaning colour (good, warn, bad) or a colour painted on the part in the 3D view. Atkinson Hyperlegible carries everything at modest sizes; hierarchy comes from weight, not scale.

Density is calm but working: 14px controls, 13px hints, 14px vertical rhythm inside a pane. Detail lives behind closed disclosures ("More information", "Which nozzle?"), so the panel stays short.

**Key Characteristics:**
- Three-zone grid (step rail, 3D stage, step panel) with a 56px header.
- Faint-green near-white surfaces, dark-green ink and buttons.
- Signal yellow reserved for "you are here" and focus.
- One primary button per step, at the panel foot.
- Hairline borders and 6px corners; shadows only on things floating over the 3D stage.
- Background work shown in a dark-green bar over the stage, never in the panel.

## Colors

A green-tinted neutral world with one dark-green voice, one yellow signal, and a separate, saturated vocabulary that only appears on the 3D part.

### Primary
- **Workshop Green** (accent): all primary buttons, the selected level and segment, links, disclosure summaries, focused finding borders, the drop-zone hover border, the selected-region colour in 3D. Hover deepens to **Bench Shadow Green** (accent-hover).
- **Pale Leaf** (accent-soft): the current step's row, the selected orientation card, a focused finding, the slicer-fix chip, the current guide choice. It is the quiet "selected" fill under green ink.
- **Night Green** (deep-green): the floating "working" bar and the 3D hover tooltip; the only dark surface in the app.

### Secondary
- **Signal Yellow** (now): the current step's dot, the focus outline (3px, 2px offset), the progress fill and spinner arc in the working bar, the pulse that flashes a changed control, and text selection (at 50% alpha).

### Tertiary (meaning)
- **Good** / **Good Soft**: completed step dots, "Prints as designed" summary, fine verdicts, "recommended" tags, strength gains.
- **Warn** / **Warn Soft**: warning badges, "shape will differ" groups, borderline verdicts, the unit hint.
- **Bad** / **Bad Soft**: problem badges, "will fail" groups, likely-to-break verdicts, error status lines.

### Part colours (3D view only)
Read by `viewer.js` from CSS custom properties, so the 3D view and the legend can never disagree. **Prints Fine** grey (part-ok), **May Droop** amber (part-borderline), **Needs Support** red (part-support, also cut previews and load arrows), **On the Bed** green (part-bed), **Held Fixed** blue (part-fixed), **Pushed** violet (part-force), **Clean Surface** teal (part-clean, also add previews), **Vertical** orange (part-vertical). These appear in the panel only as small legend squares, swatches and dots next to the list item they key.

### Neutral
- **Ink** (text): all body text and headings.
- **Muted Ink** (muted): hints, leads, inactive steps, unselected level labels, secondary lines.
- **Panel White** (panel): the right panel, inputs, secondary buttons, cards.
- **Chrome** (chrome): header and step rail.
- **Soft** (soft): drop zone, info boxes, neutral badges, verdict default.
- **Stage Grey** (stage): the 3D view background. The bed plate and grid are drawn a shade darker in the same green-grey family (#dde3df plate, #bac4bd / #ccd4ce grid).
- **Line** / **Line Strong**: hairline dividers and card borders / control borders and dashed outlines.

### Named Rules
**The One Signal Rule.** Signal Yellow means "you are here" or "keyboard focus" (plus progress inside the working bar). It is never a decoration, a heading colour or a warning colour; warnings use Warn.

**The Part Palette Stays on the Part Rule.** The saturated part colours belong to the 3D view and its keys. They never fill buttons, panels or headings.

**The Read From CSS Rule.** Any colour the 3D view paints is a CSS custom property read at runtime. No colour is defined twice.

## Typography

**Display Font:** none (no separate display face)
**Body Font:** Atkinson Hyperlegible (self-hosted, 400 and 700, with system-ui, sans-serif)

**Character:** One hyperlegible face in two weights. It reads as a careful, friendly tool for hobbyists rather than an engineering console; hierarchy comes from 700 weight and a short size ramp.

### Hierarchy
- **Headline** (700, 23px, 1.2): the step title at the top of the panel ("Your part", "Problems"), and the guide dialog title.
- **Brand** (700, 18px): the name in the header, with a 12px muted version tag.
- **Result statement** (700, 17–19px): the one-line verdict in a verdict or summary box ("Prints as designed"); coloured by meaning.
- **Title** (700, 15px): sub-headings and problem group heads inside a pane.
- **Body** (400, 15px, 1.45): leads and running text.
- **Control** (400, 14px): buttons, selects, inputs, list rows, findings.
- **Label** (700, 14px): field labels, choice labels, primary button text, selected segments.
- **Hint** (400, 13px): muted hints, notes, status line, "what this updates".
- **Badge** (700, 12px): severity badges, fix chips, tags.

Numbers in inputs, tables and timers use tabular figures.

### Named Rules
**The Weight Not Size Rule.** The ramp tops out at 23px. Emphasis is 700 weight or a meaning colour, never a bigger size.

**The Sentence Case Rule.** Headings, labels, buttons and badges are written in sentence case. No uppercase labels, no letter-spaced small caps.

## Layout

Desktop is a full-height CSS grid: 56px header across the top; below it a 210px step rail, a flexible 3D stage, and a 410px panel. Expert drops the rail (0px) and widens the panel to 480px, stacking every pane in one scroll separated by a hairline and 30px gap. The panel scrolls internally (22px 24px 28px padding); its foot (status line, Back on the left, the primary on the right) is fixed below the scroll.

Inside a pane, blocks stack in a column with a 14px gap; rows of controls wrap with 8–10px gaps. Overlays on the stage (part label, colour key, FEA legend) sit 12px from its corners; the working bar centres 16px above its bottom, at most 520px wide.

At 820px and below the grid becomes one column: header (the level switch wraps to its own full-width row), a horizontal step strip (only the current step shows its name), the 3D stage sticky at 42vh, then the panel, whose foot sticks to the bottom of the screen. Buttons and selects grow to a 40px minimum height; the colour key hides.

## Elevation & Depth

Flat by default. Zones are separated by tone (chrome vs panel vs stage) and 1px hairlines, not shadows. Shadows appear only on elements that float above the 3D stage or the page, and they are soft, green-black and ambient.

### Shadow Vocabulary
- **Floating bar** (`box-shadow: 0 8px 24px #10241c40`): the working bar over the stage.
- **Stage overlay** (`box-shadow: 0 2px 8px #1d2b271f`): the FEA colour legend.
- **Dialog** (`box-shadow: 0 12px 40px #10241c4d`, backdrop `#10241c66`): the start guide.

### Named Rules
**The Float Only Rule.** A shadow means "this hovers over something". Cards, buttons and inputs in the panel never carry one.

## Shapes

Small, even corners everywhere: 6px on buttons, inputs, cards, step rows, info boxes and stage labels. The level switch is a 9px track holding 6px segments. Larger floating things get slightly rounder: 10px for the working bar and drop zone, 12px for the guide dialog. Badges, chips and tags are full pills; step dots are circles (26px). Colour keys and swatches are near-square (2px). Borders are 1px hairlines; dashed borders mark something not yet there (the drop zone at 2px, locked step dots at 1.5px, unfinished features). A selected card switches to a 2px green border.

## Components

### Buttons
Quiet and firm: a white button with a hairline border, a solid green one when it is the step's next move.
- **Shape:** gently rounded (6px).
- **Primary:** Workshop Green fill, white 700 text, 9px 16px. One per step, at the right of the panel foot, phrased as the next move ("Next: Will it hold?"). The full-width big variant (13px padding, 16px text) is used for the single final action, such as the 3MF download.
- **Hover / Focus:** primary deepens to Bench Shadow Green; secondary buttons turn their border green; transitions 0.12s on border and background. Focus is the 3px Signal Yellow outline.
- **Secondary:** panel white, Line Strong border, 14px text, 6px 12px. Small variant 3px 10px at 13px for sample picks and list actions.
- **Ghost:** no fill, Line border, muted text that darkens on hover; on the dark working bar it inverts to a pale outline.
- **Link button:** bold green underlined text, no box, for in-flow actions.
- **Disabled:** 55% opacity, not-allowed cursor.

### Segmented choices
The level switch (Simple / Basic / Advanced / Expert) and in-pane choices ("What matters most?") are radio groups styled as segments. The level switch sits in a grey track with borderless segments; in-pane segments are separate bordered buttons. Selected: Workshop Green fill, white 700 text. A changed choice flashes a brief yellow ring (0.5s pulse).

### Chips and badges
- **Severity badge:** pill, 12px 700. Problem = Bad on Bad Soft, Warning = Warn on Warn Soft, Tip = Muted on Soft.
- **Fix chip:** pill naming the kind of fix: slicer (green on Pale Leaf), geometry (violet on pale violet), process (teal on pale teal).
- **Tag:** Good on Good Soft pill, e.g. "best" on an orientation option.
- **Estimate badge:** Muted on Soft pill, 12px 700, a button next to a heading whose numbers are estimates (strength, design changes). A click shows one sentence on what that means. No frame around the section.

### Cards / Containers
- **Corner Style:** 6px.
- **Background:** Panel White; selected or focused cards take Pale Leaf with a green border.
- **Shadow Strategy:** none (see Elevation).
- **Border:** 1px Line; Line Strong on hover.
- **Internal Padding:** 9px 11px (findings), 10px 12px (orientation options).
- **Verdict and summary boxes:** borderless, filled with the soft meaning colour, a 17–19px bold statement in the strong meaning colour, then one plain sentence.

### Inputs / Fields
- **Style:** Panel White, 1px Line Strong border, 6px corners, 14px text, 5px 7px padding. Labels stack above in 14px 700. Native checkboxes and ranges take the green accent colour.
- **Hover:** border darkens to Muted Ink.
- **Focus:** the 3px Signal Yellow outline.

### Navigation (step rail)
A checklist, not a menu. Each step is a full-width row: a 26px circle and the step name. Upcoming steps show a grey numbered dot; the current step has a Pale Leaf row, bold name and a Signal Yellow dot; done steps have a Good dot with a white check; a step being recomputed shows a spinning green arc; locked steps show a dashed empty circle and muted text. On phones the rail becomes a horizontal strip.

### Working bar (signature)
Whenever the engine runs, a Night Green bar floats over the bottom of the 3D stage: spinner with a yellow arc, what is running in bold, elapsed time in tabular figures, a Cancel ghost button, a 5px yellow progress line, and a pale-yellow line saying which steps it will update. It never moves into the panel, so it stays visible whatever step is open.

### Stage overlays
The part label and colour key sit top-left on translucent Panel White pills (6px); keys use 10px near-square swatches with plain-language names ("Prints fine", "Needs support").

### Drop zone
2px dashed Line Strong border, 10px corners, Soft fill, centred bold "Drop an STL, 3MF or STEP file here" with a muted second line; hover or drag-over turns the border green and the fill Pale Leaf.

## Do's and Don'ts

### Do:
- **Do** keep the three zones; a new step adds a pane to the panel and a row to the rail, not a new page.
- **Do** give each step one primary button, at the right of the panel foot, phrased as the next move.
- **Do** use Signal Yellow (#f2c12e) only for the current step, focus and progress.
- **Do** say what a change updates, in a 13px muted line under the control or in the working bar.
- **Do** put detail in closed disclosures with a bold green summary.
- **Do** read every 3D colour from a CSS custom property.
- **Do** use soft meaning fills (good, warn, bad) for verdicts and badges, with the strong colour on the text.

### Don't:
- **Don't** add gradients, a cutting-mat texture, or any pattern behind the 3D view; the stage stays a flat calm grey.
- **Don't** put shadows on panel cards, buttons or inputs; shadows are for things floating over the stage or page.
- **Don't** use the part colours (support red, droop amber, force violet…) as UI fills or text colours outside keys and swatches.
- **Don't** raise headings above 23px or add a second typeface.
- **Don't** add uppercase or letter-spaced labels above headings.
- **Don't** show background work only in the panel; it belongs in the working bar over the stage.
