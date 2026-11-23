# Bed adhesion for T-390

Research note, 2026-11-23. Scope: adhesion while a print is still on a hot bed. The available evidence does **not** support a numerical material-by-surface table of peel energies or pull-off strengths. Do not turn the forces below into a production threshold or give an uncalibrated prediction a pass/fail label.

## What is already known in this project

The T-390 note records a failed calibration: the positive pull in a 5 mm corner patch in the existing simulation saturates after a few bar heights, so 50–160 mm bars produce roughly the same normalized corner pull, even though longer bars warp more. A threshold on that patch therefore cannot reproduce the reference-bar behavior. The note also records that the user's copy of Kaščak et al. was read previously; the paper was attributed there to Hudák, but Crossref identifies the authors as Kaščak et al. The note says 12 tests per surface, PETG only, and that neither footprint area nor loading direction was sufficiently clear to obtain a comparable stress. These numbers below are **transcribed from that project note**, not independently checked against the PDF in this research.

`warp.simulate` progressively solves a voxelized, fully clamped bed, returns signed vertical nodal `pull` in N (positive away from the bed), grid spacing `h` in mm, and free-release displacement `u`. `lift_stress` divides each positive nodal force by h², giving nominal MPa; this is not a measured local interface strength, and boundary-node tributary areas and mesh sensitivity matter. `curl` removes rigid tilt and gives the range of released bed-node displacement in mm, not lift during printing. The result is for a fully attached interface: it does not allow an edge crack to advance or recompute after lift. The thresholds file contains empirical risk lengths, not adhesion measurements.

## Measurements located

| Material and surface | Reported force, mean ± spread | Test and conditions | Usefulness |
|---|---:|---|---|
| PETG, glass + polyimide film | 58.0 ± 5.7 N | Printed block pushed off; 12 tries, bed temperature and contact area not established from available note | Relative comparison in this setup only; loading direction and meaning of spread need verification |
| PETG, borosilicate glass | 54.7 ± 12.1 N | Same | Same |
| PETG, PEI on spring steel | 53.5 ± 8.0 N | Same; smooth versus textured not established | Same |
| PETG, bare aluminium | 33.1 ± 5.5 N | Same | Same |

Source: J. Kaščak, M. Kočiško, A. Vodilka, J. Török and T. Coranič (2024), “Adhesion Testing Device for 3D Printed Objects on Diverse Printing Bed Materials: Design and Evaluation,” *Applied Sciences* 14(2), 945, https://doi.org/10.3390/app14020945. Direct full text was not independently accessible in this session: **read by hand**, especially the diagram, test direction, temperatures, specimen dimensions, and what the ± denotes. These values are not peel energies; do not divide by a guessed footprint. If a reliable footprint A is obtained, average nominal stress is F/A in N/mm² = MPa, but only for the reported loading mode. Peak force divided by footprint is not local crack-tip stress.

Laumann et al. study PLA build-plate adhesion using 90° peel and printing speed/temperature; the full text was unavailable here, so numerical values, substrate, width, and temperature cannot be checked. **Read by hand** before using it. Pollák and Goryl evaluate FFF/FDM print surface adhesion; full text was unavailable here. **Read by hand** before extracting their conditions or values. These are leads, not evidence for invented defaults.

## Recommended model

Use an interface fracture calculation, with explicitly measured, temperature- and mode-dependent effective fracture energy Gc, and an initial edge flaw. At each printing increment, allow a small attached area ΔA at a candidate edge to debond, re-solve equilibrium using the same accumulated eigenstrain and current stiffness, and compute

`G = -(Π(a + Δa) - Π(a)) / ΔA`, in N/mm; `1 N/mm = 1000 J/m²`.

Here Π is total elastic potential energy under the same loads and thermal state; compute the before and after energies consistently, including eigenstrains and external work, not from free-release curl or nodal force alone. Advance when `G >= Gc`, remove the corresponding interface constraints, then re-solve and repeat; a real corner has mixed opening and sliding modes, so a mode-mix criterion and friction/contact treatment may be required. The grid sets the minimum crack increment and effective initial flaw; test mesh convergence and calibrate against printed bars of several lengths, orientations and surfaces. A finite crack and constraint release are essential: a point-stress threshold inherits the observed saturation and can be mesh dependent. Current `held_total` contains all three reaction components internally, but only the normal pull is returned; accumulated shrink history and state would need to be retained for a consistent energy comparison. A temperature-dependent, viscoelastic/cohesive treatment would be more realistic; the proposed elastic method is a screening approximation, not a validated prediction.

For an independently measured flexible strip peeled at angle θ under steady force F and width b, Kendall's ideal elastic peel model gives `Gc = (F/b)(1 - cos θ) + F²/(2 E t b²)`, where E is tensile modulus and t strip thickness. At 90°, the first term is F/b. This strip formula is a way to interpret a suitable peel test, not a formula that directly converts this project's restrained corner force into Gc. Actual plastic dissipation, bending, rate and test temperature affect an effective measured value. Source: K. Kendall (1975), “Thin-film peeling—the elastic term,” *Journal of Physics D: Applied Physics* 8, 1449–1452, https://doi.org/10.1088/0022-3727/8/13/005. Citation and model are classical; transfer to printed corners is an engineering proposal requiring validation.

## Defaults and practical factors

| Material | Textured PEI | Smooth PEI | Glass | Glass + glue | PI/Kapton | BuildTak-like | Bare aluminium |
|---|---|---|---|---|---|---|---|
| PLA | unknown | unknown | unknown | unknown | unknown | unknown | unknown |
| PETG | unknown | unknown | unknown | unknown | unknown | unknown | unknown |
| ABS | unknown | unknown | unknown | unknown | unknown | unknown | unknown |
| ASA | unknown | unknown | unknown | unknown | unknown | unknown | unknown |
| TPU | unknown | unknown | unknown | unknown | unknown | unknown | unknown |
| PC | unknown | unknown | unknown | unknown | unknown | unknown | unknown |

“Unknown” means no defensible numerical default was established here, not zero grip. Permit expert-entered measured Gc with its test temperature and mode; otherwise retain the existing empirical warp-risk estimate and identify adhesion as unknown. Do not silently equate ASA with ABS, or PLA with PLA+.

Bed temperature changes wetting, polymer stiffness and cooling stress at the same time; its effect on measured grip is material- and surface-specific and need not be monotonic. The existing heat/shrink calculation already responds to bed temperature, so an additional universal multiplier would risk double-counting. A brim extends attached area and moves the crack front; model its actual printed geometry, continuity, gaps and interface, rather than multiply Gc by brim area. Glue can increase adhesion or act as a release/barrier layer depending on material, bed, formulation and temperature; represent it as a different interface, not a universal multiplier. No defensible numeric multipliers were found in the accessible evidence.

Prusa Research, “Steel sheets” (manufacturer guidance on material/sheet combinations and release layers), https://help.prusa3d.com/article/steel-sheets_2195; “Warping” (temperature and brim guidance), https://help.prusa3d.com/article/warping_2011. These are practical guidance, not measured fracture energies. Check the current material-specific sheet guide before recommending glue: an overly strong bond can damage a surface.

## Sources to read by hand

- D. Laumann, D. Spiehl, E. Dörsam and A. Blaeser (2023; online 2022), “Interaction of printing speed and temperature settings on the build plate adhesion of 3D-printed polylactide acid parts,” *The Journal of Adhesion* 99, 1186–1204, https://doi.org/10.1080/00218464.2022.2093640. Crossref metadata checked; full text unavailable. Record peel force per width, temperatures, surface, specimen and rate before applying Kendall.
- M. Pollák and K. Goryl (online 2025), “Evaluation of print surface adhesion in FFF/FDM,” *International Polymer Processing* 41, 143–150, https://doi.org/10.1515/ipp-2025-0113. Crossref metadata checked; full text unavailable.
- Kaščak et al. above: recover the user's PDF to verify the inherited measurements and whether force-to-area conversion is actually possible.

Confidence: high that force, stress and fracture energy cannot be interchanged without the missing geometry and test mode; moderate in the proposed energy-release framework as a physical direction; low in quantitative predictions for hobbyist printers until interface measurements, crack geometry, thermal history, grid convergence and independent print outcomes are validated. There are no evidence-backed numeric defaults in this note.
