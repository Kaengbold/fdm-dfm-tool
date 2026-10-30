# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users
Hobbyists with a consumer FDM printer (Bambu, Prusa, Creality, Voron…) who slice in OrcaSlicer, Bambu Studio or PrusaSlicer. They have a part (their own CAD or a download) and want it to print well and hold, without learning engineering terms. Some can change the design, many can only change slicer settings.

## Product Purpose
Drop in a part, get the best orientation, what will go wrong, slicer settings that fix it, a strength check, and (if the design can change) previewable geometry fixes. Success: the first print works and holds.

## Positioning
Runs entirely in the browser, free and open source (AGPL-3.0-or-later), no account, no upload. Every check is deterministic code; AI is optional. Exports a 3MF with per-part settings, so the user's own slicer profiles stay theirs.

## Operating Context
Used at a desk next to the printer, between CAD and slicer, on desktop mostly. Phones only for Simple and Basic. Parts arrive as STL, 3MF or STEP.

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
