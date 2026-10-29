# Research jobs for Gemini (or any helper)

Each job is self-contained: paste the prompt, save the answer as the named file in `research/`.
Rules for every job (paste them along):

> Use only sources you actually opened; give the URL for every row. If you are not sure, write
> "unsure" instead of guessing. No marketing language. Output exactly the format asked for.

---

## G-1 OrcaSlicer settings most users don't know (→ `research/orca_features.md`, T-152)

> Go through the OrcaSlicer wiki (https://github.com/SoftFever/OrcaSlicer/wiki) and the settings
> in the OrcaSlicer source (`src/libslic3r/PrintConfig.cpp`). List settings that improve print
> quality, strength or accuracy but are off by default or hidden in "Advanced/Expert" mode.
> Markdown table: setting key (exact config name) | name in the UI | what it does in one plain
> sentence | when it helps (which kind of part or problem) | default value | per-object setting? (yes/no/unsure) | source URL.
> Examples to include if they exist: polyholes, precise wall, scarf seam, fuzzy skin, ironing,
> interlocking beams, brim ears, make overhangs printable, arc fitting, small-perimeter speed, hole compensation.

## G-2 The same for PrusaSlicer (→ `research/prusa_features.md`, T-152)

> Same table for PrusaSlicer (help.prusa3d.com knowledge base and `src/libslic3r/PrintConfig.cpp`
> in github.com/prusa3d/PrusaSlicer). Add a column "OrcaSlicer equivalent key" where one exists.

## G-3 Filament data sheets (→ `research/material_data.md`, T-171, T-104)

> For PLA, PLA+, PETG, ABS, ASA, PC, PA-CF (nylon carbon fibre) and TPU 95A, collect from the
> manufacturers' technical data sheets (Bambu Lab, Prusament, Polymaker, eSun; at least two brands each):
> tensile strength XY and Z (MPa, and the test standard), Young's modulus (MPa), heat deflection
> temperature (°C and load), glass transition (°C), density (g/cm³), shrinkage if given, recommended
> nozzle/bed temperature, typical price per kg (EUR). One table per material, one row per brand, URL per row.

## G-4 Free calibration prints (→ `research/calibration_prints.md`, T-170)

> Find well-known calibration test prints for: hole/peg tolerances and fits, overhang angle,
> bridging distance, support Z gap, layer adhesion strength, warping, dimensional accuracy, cooling.
> Table: what it measures | name | author | licence (exact, e.g. CC BY 4.0) | link | how the result is read
> (what the user measures or looks at). Mark licences that don't allow sharing or changes.

## G-5 CAD tricks for FDM (→ `research/cad_tricks.md`, T-181)

> List design tricks that make FDM parts print better or stronger, from sources like CNC Kitchen,
> Slant 3D, Maker's Muse, Prusa knowledge base, Teaching Tech: e.g. teardrop holes, chamfers instead
> of fillets on overhangs, sacrificial bridging layers, holes that force extra perimeters, ribs and
> gussets, splitting big parts, print-in-place clearances, captive nuts, elephant-foot chamfers.
> Table: trick | problem it solves | when to use it | typical numbers (sizes, angles) | source URL.

## G-6 Check our research links (→ `research/link_check.md`, T-020)

> For every URL in the attached file (`research/fdm_knowledge.md`): open it and report
> URL | opens? (yes/no) | does the page support the claim next to it? (yes/partly/no) | one-line note.

## G-7 Printer facts (→ `research/printer_data.md`)

> For these printers: Bambu Lab A1, A1 mini, P1S, X1C, H2D; Prusa MK4S, MINI+, CORE One, XL;
> Creality K1C, K2 Plus, Ender-3 V3 SE, KE; Elegoo Centauri Carbon; Anycubic Kobra 3 — from the
> manufacturer's spec page: build volume (mm), enclosure (none/passive/heated chamber), heated bed max °C,
> nozzle sizes offered, hardened nozzle standard? (yes/no), max print speed claimed, multi-material option. URL per row.

## G-8 Which OrcaSlicer settings work per object in a 3MF (→ `research/orca_per_object.md`, T-136)

> In the OrcaSlicer source, find which print settings can be set per object or per modifier
> (stored in `Metadata/model_settings.config` of a project 3MF) and which only per plate/print.
> List the per-object keys. Source: code file and line links.

---

What not to give a helper: anything that changes code, decides thresholds, or needs judging
whether a model is right. Those need checking against tests and real prints.
