---
name: fdm-check
description: Check a part for FDM 3D printing with the fdm-dfm MCP server (or the fdmdfm command line) - best orientation, print problems, strength under a load, warping, slicer settings, design changes, a 3MF for the slicer and a report with pictures. Use when the user wants to 3D print an STL or 3MF part, asks whether it will print or hold, or wants print settings.
---

# Checking a part for 3D printing

The `fdm-dfm` MCP server runs the FDM Check engine on the user's computer. No AI runs inside it: you
do the talking, it does the geometry and the numbers. Without MCP, the `fdmdfm` command line does the
same (`fdmdfm --help`; JSON on stdout, errors as one line on stderr).

## The usual flow

1. **Ask what it's for** if the user hasn't said: printer, material, what holds it and what pushes
   on it, how warm it gets. Don't guess loads; a part with no load needs no strength check.
2. `list_presets` → ids for `printer`, `material`, `nozzle`. Pass the same three to every later call.
   The user's own printer or filament: `set_custom_presets`.
3. `load_part(path)`. Check `unit_hint` (a part exported in inches is 25.4× too small: `scale_part`)
   and `bodies` (several bodies: `select_body`).
4. `find_orientations` → pick the first unless the user has a reason. Its `up` is the build
   direction you pass to everything else.
5. `check_print(up)` → findings (each with plain `title`, `detail`, `why` and `fixes` for the design,
   the slicer or the printer and material), print time, filament, recommended slicer settings.
   `mode`: "print" when the model can't be changed, "design" when slicer settings can't.
6. Loads (only if it carries something): `list_faces` → face ids, then `check_strength(up, loads)`.
   Not happy? `strength_options` (what helps most) or `optimise_strength` (lightest settings that hold).
7. Design changes (only if the user can change the model): `suggest_design_changes` →
   `apply_design_changes` saves a new STL and loads it; check again. For a redesign,
   `suggest_lighter_shape(up, loads, path.stl, keep=0.4)` saves a blocky shape that keeps the plastic only
   where the load runs: a guide for the user to rebuild in CAD, not a part to print as it is.
8. `export_3mf(up, path)` → a 3MF with the part turned and the settings set, for OrcaSlicer / Bambu
   Studio (`slicer: "orca"`) or PrusaSlicer (`slicer: "prusa"`).
9. Show it: `write_report(path.html, up, loads)` for the user to open, `render_image` to see the part
   yourself (bed, overhangs, problem close-up, stress, warp, compare) and `make_chart` for one chart.

## Units and what the numbers mean

- Lengths in **mm**, forces in **kg** (the weight it carries, 1 kg ≈ 10 N), temperatures in **°C**,
  `infill` 0–1, `walls` in lines, time in minutes, filament in grams.
- `up`: a direction in the part's own coordinates that will point up on the printer.
- **Safety factor**: how many times the load the part holds before it breaks. Aim for ×2, ×3 when
  the load stays on (`constant_load`) or the part gets warm. Below ×1 it breaks.
- Strength, warping and heat are **rough estimates** from a coarse simulation: good for comparing
  options, not for safety-critical parts. `fine=True` or a sliced G-code (`load_gcode`,
  `detailed_strength`) gets closer.
- Severity: "problem" (fix it before printing), "warn" (check it), "info" (a tip).

## Talking to the user

- Plain words for hobbyists: "the hole will sag at the top", not "unsupported horizontal bore".
- Lead with the answer (prints fine / needs support here / will break at the clip), then the fix.
- Give a fix the user can do: a slicer setting is easiest, then printer or material, then the design.
- Say "about" for times, weights and safety factors, and that the first print is the real test.
- Give file paths for everything you saved (3MF, report, pictures).
