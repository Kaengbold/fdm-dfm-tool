# Bed adhesion for T-390

Research note, 2026-11-23. Scope: adhesion while a print is still on a hot bed. The available evidence does **not** support a numerical material-by-surface table of peel energies or pull-off strengths. Do not turn the forces below into a production threshold or give an uncalibrated prediction a pass/fail label.

## What is already known in this project

The T-390 note records a failed calibration: the positive pull in a 5 mm corner patch in the existing simulation saturates after a few bar heights, so 50–160 mm bars produce roughly the same normalized corner pull, even though longer bars warp more. A threshold on that patch therefore cannot reproduce the reference-bar behavior. The note also records that the user's copy of Kaščak et al. was read previously; the paper was attributed there to Hudák, but Crossref identifies the authors as Kaščak et al. The note says 12 tests per surface, PETG only, and that neither footprint area nor loading direction was sufficiently clear to obtain a comparable stress. The force values below are transcribed from that project note; the paper?s method and loading direction were checked in round 2.

`warp.simulate` progressively solves a voxelized, fully clamped bed, returns signed vertical nodal `pull` in N (positive away from the bed), grid spacing `h` in mm, and free-release displacement `u`. `lift_stress` divides each positive nodal force by h², giving nominal MPa; this is not a measured local interface strength, and boundary-node tributary areas and mesh sensitivity matter. `curl` removes rigid tilt and gives the range of released bed-node displacement in mm, not lift during printing. The result is for a fully attached interface: it does not allow an edge crack to advance or recompute after lift. The thresholds file contains empirical risk lengths, not adhesion measurements.

## Measurements located

| Material and surface | Reported force, mean ± spread | Test and conditions | Usefulness |
|---|---:|---|---|
| PETG, glass + polyimide film | 58.0 ± 5.7 N | Printed block pushed off; 12 tries, bed temperature and contact area not established from paper | Relative comparison in this setup only; normal pull; nominal stress requires specimen area |
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

Confidence: high that force, stress and fracture energy cannot be interchanged without the missing geometry and test mode; moderate in the proposed energy-release framework as a physical direction; low in quantitative predictions for hobbyist printers until interface measurements, crack geometry, thermal history, grid convergence and independent print outcomes are validated. There are no evidence-backed numeric peel-energy defaults in this note; the measured shear benchmarks below are not peel thresholds.


## Round 2: measured shear benchmarks and limits

The open [Spoerk doctoral thesis](https://backoffice.biblio.ugent.be/download/8620147/8620156), chapter 4.2, reproduces Spoerk, Gonzalez-Gutierrez, Sapkota, Schuschnigg and Holzer, [Effect of the printing bed temperature on the adhesion of parts produced by fused filament fabrication](https://doi.org/10.1080/14658011.2017.1399531), *Plastics, Rubber and Composites* 47 (2018), 17?24. This is the glass/PI study sought in the prompt. The similarly named [Schuschnigg conference contribution](https://pure.unileoben.ac.at/en/publications/adhesion-of-standard-filament-materials-to-different-build-platfo/) is an abstract for PLA and ABS, not a separate PEI/PETG dataset. The author list including Lichal, Cajner, Berger and Cardon belongs instead to the PP paper below. No evidence for the proposed PLA/PETG/PEI-like/BuildTak dataset was found under that citation.

The Leoben test sheared **one** 100 mm long, three-layer strand sideways with a horizontal block; 16 strands per condition, width 1.65?1.9 mm, first layer 0.2 mm, bed held at temperature, PLA nozzle 220 ?C, ABS 255 ?C. The measured peak force divided by 100 mm is a **shear force per strand length**. This normalization is a calculation from measured means, not a direct measurement of peel energy. Approximate nominal average shear stress uses the entire apparent footprint, `F/(100 mm ? width)`; the width range produces the bounds below and does not represent true contact area or uncertainty. The published ? values are retained as the authors' reported spread; see the paper for statistical treatment.

| Material / surface | Bed ?C | Peak force N | Calculated shear force / length N/mm | Apparent average shear MPa (width 1.65?1.9 mm) |
|---|---:|---:|---:|---:|
| PLA / glass mirror | 60 | 73 ? 19 | 0.73 ? 0.19 | 0.38?0.44 |
| PLA / glass mirror | 70 | 651 ? 17 | 6.51 ? 0.17 | 3.43?3.95 |
| PLA / PI on glass | 60 | 51 ? 8 | 0.51 ? 0.08 | 0.27?0.31 |
| PLA / PI on glass | 70 | 322 ? 47 | 3.22 ? 0.47 | 1.69?1.95 |

The force increases by approximately 8.9? on glass and 6.3? on PI from 60 to 70 ?C; PLA Tg was 60.6 ?C. This is not a universal temperature multiplier: above 70 ?C the paper reports an initial fall to roughly 50?60% of the peak; PI rises again between 80 and 100 ?C. ABS (Tg 110.1 ?C) rose nonlinearly with temperature, adhered better to PI than glass, and reached the authors' 200 N benchmark on PI around 120 ?C; a peak was not observed within the equipment's range. The authors' 200 N refers specifically to their 100 mm strand and comparable printing conditions, not a universal part criterion. They observed glass damage at high PLA adhesion.

**Provisional defaults for reproducing this shear experiment only:** PLA/glass at 60 ?C 0.73 N/mm; at 70 ?C 6.51 N/mm; PLA/PI at 60 ?C 0.51 N/mm; at 70 ?C 3.22 N/mm. These are normalizations of measured means, not estimated neighbours. Other material/surface/temperature combinations, including ABS, PETG, PEI and glue, remain unknown for this metric unless independently measured. Do not interpolate across the abrupt transition or extrapolate to other surfaces. For the proposed peeling model the final defaults table above remains **unknown** in every cell: a shear peak per length is not a steady 90? peel force per width, even though both have units N/mm. A numerical conversion requires a measured mixed-mode fracture law or test-calibrated mechanical model; neither follows from these observations. Consequently no inferred neighbour estimates or production pass/fail defaults are justified.

### Other leads checked

- [Spoerk et al., *Polymers* 10 (2018) 490](https://www.mdpi.com/2073-4360/10/5/490): PP composite on polymer plates, 100 mm two-layer strands, horizontal shear block 0.1 mm above bed moving at 2 mm/s. At room temperature PP composite on homopolymer PP gave 45.0 ? 13.0 N, or 0.450 ? 0.130 N/mm of strand; on UHMW-PE it did not adhere at room temperature. In its temperature study force rose with platform temperature and was around 70 N under the reported baseline, with 0.2 mm first layer. Width changed with conditions. These PP values do not calibrate PLA/ABS/PETG peel; the article's 200 N comparator is the earlier device-specific heuristic.
- [Ka??ak et al.](https://www.mdpi.com/2076-3417/14/2/945): the full text says the motor pulls the specimen perpendicular to the bed and identifies ? as standard deviation for 12 repetitions; its table captions nevertheless say ?shear?. The testing specimen footprint is not dimensioned in the text and bed temperature is adjusted by surface without a reliably readable numeric setting. Thus no sound F/A or hot-bed default follows. The four inherited PETG means remain useful comparisons only, with the important caveat that process settings differed.
- [Spitaels et al. 2024](https://orbi.umons.ac.be/bitstream/20.500.12907/49059/1/2024-SPITAELS-ESAFORM.pdf): PLA on glass at 60 ?C, 20 ? 20 mm rounded cube, force parallel to plate, applied over a face; 95% cubic infill, no brim, 50.59 ? 1.97 N, with 1 mm displacement as failure criterion. Loading height, geometry and infill change the result. This is a part-level mixed-mode debonding benchmark, not a transferable peel energy.
- [Bechtel et al., *Materials* 13 (2020) 3371](https://www.mdpi.com/1996-1944/13/15/3371): PLA/aluminium joints printed on substrates at 150?200 ?C and assessed after a special cooling/tempering cycle. Relevant to thermal history and lap-joint adhesion, but not a hot ordinary printer-bed peel threshold.

Confidence is moderate for the transcribed PLA shear forces and their arithmetic under the reported test setup, low for transfer to ordinary first-layer printing, and insufficient for numerical corner-peel defaults. The unusually slow 50 mm/min print speed, wide strands, loading mode, surface preparation, temperature history and material formulation all affect transfer. Calibration against a measured peel test and independent bars remains necessary.

## Screening model used in T-390

The checker uses the shear measurements as inputs to an editable grip estimate. A triangular traction-slip curve gives `Gc ≈ (F/L) × 0.30 mm / (2 × 1.8 mm)`, with 1.8 mm the approximate strand width and 0.30 mm an assumed slip distance. All resulting peel-work values are low confidence. The Expert table lists the PLA shear measurements separately from the material and plate estimates. The latter borrow a nearby PLA or ABS-family estimate and use the Kaščak comparison for relative plate grip.

At the end of the clamped simulation, the checker tries a seed patch at the strongest upward edge reaction, followed by up to two front advances. At fixed thermal state, applying the reversed clamp reactions as loads about the held equilibrium gives released energy `W = f·u/2`. Each trial compares `(W_new − W_old)/ΔA` with `Gc`, using the bottom faces to weight the released area. A corner flags when the opening front advances at least 2 mm beyond the starter patch.

The experimentally uncalibrated screening model also requires the calculated free curl to reach two profile layer heights. The local energy release by itself plateaued between the 40 mm and 90 mm ABS bars. At 1 mm voxel spacing, free curl was 0.29 mm and 1.20 mm respectively. The opening screen and peel solve together flagged the 90 mm bar on an open printer. The solver checks the final shrink state of the bare model and assumes linear elasticity. Testing against real hot-bed prints remains open.
