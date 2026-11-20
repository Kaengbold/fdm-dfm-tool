# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users
Hobbyists with a consumer FDM printer (Bambu, Prusa, Creality, Voron…) who slice in OrcaSlicer, Bambu Studio or PrusaSlicer. They have a part (their own CAD or a download) and want it to print well and hold, without learning engineering terms. Some can change the design, many can only change slicer settings.

### Use cases to design for
New features name which of these they serve.
1. **Beginner with a downloaded model**: "will this print on my A1?" → a verdict, print time, grams, one file to open, no CAD words.
2. **Functional maker with their own CAD**: a bracket, hook or enclosure that must hold → strength, fits, design advice to take back to CAD.
3. **Tinkerer with a tuned profile** → a real difference to their own profile, with numbers, to pick and choose from.
4. **Small seller or print farm**: the same part many times → failure risk, time and cost, saved projects.
5. **Teacher or club** → a report to share, with plain reasons.
6. **Agent user** (drives the tool from Claude Code, Codex or similar) → an easy local install.

## Product Purpose
Drop in a part, get the best orientation, what will go wrong, slicer settings that fix it, a strength check, and (if the design can change) previewable geometry fixes. Success: the first print works and holds.

## Positioning
Runs entirely in the browser, free and open source (AGPL-3.0-or-later), no account, no upload. Every check is deterministic code; AI is optional. Exports a 3MF with per-part settings, so the user's own slicer profiles stay theirs.

## Operating Context
Used at a desk next to the printer, between CAD and slicer, on desktop mostly. No phone app (decided 2026-11-15): the file to check lives on a computer, and prints started from a phone are pre-sliced and need no check. On a phone the site has to do two things well: read a shared report, and the failed-print path at the printer. Everything else only has to work there. Parts arrive as STL, 3MF or STEP.

## Capabilities and Constraints
- Four levels: Simple (printer and material, optional load, one result), Basic (all choices, no deep simulation), Advanced (everything, step by step), Expert (everything on one long page, later).
- Step-by-step flow for Simple, Basic and Advanced: part, what you have, orientation, problems, strength, design changes, slicer settings, download.
- Background work must always say what runs, how long, what it will update, and allow cancel. A change must say what it updates.
- Short text; explanations only behind "More information".
- English only for now.
- Engine runs in a web worker (Pyodide); static site, no build step, plain ES modules.
- Simulations must leave the computer usable (video, browser) while they run: one web worker (one core) in the browser, maths threads capped at half the cores when run natively, bounded memory.

## Brand Commitments
Name for now: FDM Check (working title "FDM DFM Tool"). Plain language for hobbyists.

## Evidence on Hand
Sample parts in `web/samples/`. No user testimonials, no benchmarks: do not invent any. Strength results are estimates and must be labelled so.

## Product Principles
1. Zero install, zero account.
2. Plain language first, detail on request.
3. Rough but honest: simulations are indications.
4. One clear next step at any moment.
5. Show cause and effect: what a change updates.
6. Show first what only this tool can do (decided 2026-11-15, T-431). What the slicer does better (print time, painting supports, arranging a plate, cutting, textures) stays reachable but comes after it, with a line saying the slicer can do it too.
