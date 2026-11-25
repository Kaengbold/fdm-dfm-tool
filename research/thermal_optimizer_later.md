# Thermal optimizer follow-ups (T-484)

## Bambu side fan

Bambu's auxiliary part-cooling fan blows across the print from the side. P1S, X1 Carbon and H2D have this blower. A1 and A1 mini profiles exclude it. The G-code reader keeps P1 (toolhead fan), P2 (side fan) and P3 (chamber exhaust) separate. The model counts P2 on equipped printers. P3 remains in the file and in the recorded commands. Estimating its effect would require a chamber air exchange model and inlet temperature. [Bambu Lab P1/X1 fan](https://eu.store.bambulab.com/products/auxiliary-part-cooling-fan) · [Bambu Lab H2-series fan](https://eu.store.bambulab.com/products/auxiliary-part-cooling-fan-h2d) · [Bambu Studio machine profiles](https://github.com/bambulab/BambuStudio/tree/master/resources/profiles/BBL/machine)

The estimate adds 20 W/m²K at full P2 speed, with 10–40 W/m²K as a plausible range. The model's existing 10 W/m²K still-air term and 120 W/m²K full part-fan term add to 130 W/m²K. The 20 W/m²K increment reaches the upper end of Costa et al.'s published FFF convection bracket of 5–150 W/m²K. We scale the increment linearly with PWM, reuse the existing 0.4 s fan time constant and 30 mm height falloff, and apply it to the exposed cells near the active layers. Confidence is low. The vendor describes the airflow direction and hardware, and Costa brackets typical convection. The increment, PWM curve and uniform coverage across the bed are model assumptions. A calibration should compare fan-off and fan-on cooling curves on both sides of the bed, at several heights, with the part fan held fixed. [Costa, Duarte and Covas (2014)](https://doi.org/10.1080/17452759.2014.984042)

The optimizer can try a temporary P2 boost on supported Bambu printers for PLA. The G-code rewrite restores the sliced setting when a boost ends, including at the end of printing. It re-reads the fan levels and repeats the heat check before offering the download.

## Prusa binary G-code

The reader validates block checksums and decodes Deflate, both Heatshrink window sizes and both MeatPack encodings. It brings producer, print estimates and slicer settings into the text as comments. The plain download carries commands and the comments present in the input. It omits binary thumbnails. MeatPack files made without profile or feature comments cannot recover the details the slicer discarded. The regression fixture is a PrusaSlicer 2.8 binary export of the repository's `core/tests/gcode/part.stl`. Its decoded paths, extrusion and time were compared against a plain export of the same slice. [Prusa binary G-code specification](https://github.com/prusa3d/libbgcode/blob/main/doc/specifications.md)

## Finer voxel tier for hot spots

Keep the existing quick solve and its local fine reruns. Those already cover flagged areas with 1 mm in-plane cells instead of 2 mm. The cropped run still uses the original deposition timeline and discards findings in its outer margin. It omits heat flow across the crop boundary. Refining through the thickness would need a way to transfer temperature and heat flux across that boundary and represent bead contacts, otherwise the extra cells can change the estimate for the wrong reason.

An adaptive octree can provide high resolution around a moving heat source. Published work demonstrates this approach for extrusion additive manufacturing, and finite-difference work compares simulated cyclic heating with infrared measurements. The current sparse solve lives in Python through SciPy. A WebGPU solver would need its own kernels, worker integration, GPU buffer and precision checks, and a CPU path for local mode and browsers without WebGPU. The papers establish a method to benchmark. Performance and accuracy still need measurement on our toolpaths and browsers. [Gamdha et al. (2025)](https://doi.org/10.1016/j.finel.2025.104410) · [Finite-difference validation](https://pubmed.ncbi.nlm.nih.gov/39359581/)

Follow-up prototype in NumPy/SciPy. Run the quick model over the whole print, use its temperatures on the boundaries of a padded hot region, and refine occupied cells around the flagged bead contacts. Compare against the current local fine result and measured cover temperatures on a pin, a bridge and a thick-to-thin transition. Check whether the hot and weak spot classifications improve, and record wall time and peak memory under native Python and Pyodide. Use those measurements to decide whether a GPU implementation would help.

### T-604 prototype results

`thermal.refine_hot_region(tp, timing, profile, box)` takes a caller-selected hot area in printer XY. It pads the box by 2 mm and snaps it outward to the 2 mm quick grid. The whole-print run records the temperatures of occupied coarse cells just outside the four cut faces at every solver step. The local 0.5 mm grid interpolates those temperatures in time, starts each exterior cell at its deposition temperature, and conducts heat across shared faces after both sides are deposited. The local grid keeps the original layer heights and bead-neck contact model. The coupling is one-way, with no fine-to-coarse feedback or diagonal links across cuts. The prototype caps saved history at about 16 MiB and caps each deposition and nozzle sample array at one million positions.

The cases are the repository's Orca-sliced pin, a controlled PLA toolpath with a 10 mm free bridge between piers, and a controlled PLA toolpath with a 4 mm wide cap on a thick base. Each temperature is the peak immediately before the next bead covers a cell in the same target zone. Hot and weak shares are weighted by plastic volume and use the existing temperature-window rules. Hot shares use covered cells and weak shares use part-to-part joints. Local fine uses the same padded box at 1 mm with insulated cut faces. Refined timing includes both the whole-print quick pass and the 0.5 mm local pass with coupling.

| Part | Peak cover °C, quick / local fine / refined | Hot share %, quick / local fine / refined | Weak share %, quick / local fine / refined |
| --- | ---: | ---: | ---: |
| Pin | 171.55 / 172.59 / 181.52 | 100 / 100 / 100 | 0 / 0 / 0 |
| Bridge | 108.78 / 100.45 / 101.58 | 100 / 100 / 100 | 0 / 0 / 0 |
| Thick-to-thin | 176.24 / 180.86 / 184.81 | 16 / 20 / 21.05 | 0 / 0 / 0 |

| Part | Native seconds, quick / local fine / refined | Pyodide seconds, quick / local fine / refined | Native peak RSS rise MiB | Node peak RSS rise MiB | Wasm heap growth MiB |
| --- | ---: | ---: | ---: | ---: | ---: |
| Pin | 1.06 / 2.03 / 4.35 | 2.17 / 3.90 / 7.58 | 2.7 / 4.1 / 11.5 | 6.4 / 11.4 / 11.5 | 0 / 0 / 0 |
| Bridge | 0.34 / 0.67 / 0.97 | 0.64 / 1.06 / 1.99 | 2.0 / 2.3 / 4.6 | 2.6 / 0.0 / 6.9 | 0 / 0 / 0 |
| Thick-to-thin | 0.34 / 0.64 / 1.11 | 0.71 / 1.11 / 2.17 | 2.2 / 3.3 / 7.2 | 11.8 / 3.0 / 12.9 | 0 / 0 / 12 |

Single runs in fresh processes on Windows with a Ryzen 7 1700, Python 3.10.9 and Node 18.18.0 with Pyodide 0.27.8. The native runs set `OPENBLAS_NUM_THREADS` and `OMP_NUM_THREADS` to 1. Timings exclude imports, G-code parsing and classification. Peak RSS rise is the process high-water mark after the run minus the high-water mark before the run. The Wasm numbers measure growth in allocated linear memory, including spare capacity. The refined runs ended at 71.875, 59.875 and 71.875 MiB of Wasm memory for pin, bridge and thick-to-thin, respectively. Reproduce each row with `python tools/thermal_refine_bench.py <pin|bridge|transition> <quick|fine|refined>` and `node tools/thermal_refine_bench.mjs <pin|bridge|transition> <quick|fine|refined>`. Install the Node dependency with `npm install --no-save --no-package-lock --prefix tools pyodide@0.27.8`.

**Defer WebGPU and keep the CPU prototype opt-in.** The three Pyodide runs took 2.0–7.6 s end to end and grew the Wasm heap by at most 12 MiB. The coarse pass itself took 0.62–1.99 s in the coupled runs. The change in hot share was confined to the transition, and no case triggered a weak-joint finding. The different peak predictions need measured cover temperatures and weak-joint data from real prints before they can establish an accuracy gain. Revisit GPU work if those measurements support refinement and larger, representative crops exceed the CPU time or memory budget.

## Island order as a slicer hint

A slicer can give a hot island more time to cool by choosing another island in the same object as the next stop. Candidate islands must already be printable on the same layer with the same material. Preserve the seam start and loop direction of each island, and let the slicer replan travels, retractions, wipes and lifts against the plastic deposited so far. Keep bridge and support dependencies and object labels intact. Disable the hint for material changes, purge sequences and sequential-object printing. PrusaSlicer's seam and travel controls illustrate why the slicer needs to own those decisions. [Prusa seam position](https://help.prusa3d.com/article/seam-position_151069) · [Prusa layers and perimeters](https://help.prusa3d.com/article/layers-and-perimeters_1748)

A useful hint would identify the object, layer and hot island, plus the minimum additional time before returning. The slicer could accept it only when its own travel planner finds a clear route and the extra travel costs less than a cooling wait. Replay the resulting toolpath through the heat model and check travel clearance, seam positions and unchanged extrusion. A file already sliced carries modal commands and can lack the island-level constraints needed to prove an alternate order safe.

Suggested app text when it has enough information to identify a candidate: “This spot could use more time to cool. If your slicer supports it, try printing another section of the same layer before returning here.”
