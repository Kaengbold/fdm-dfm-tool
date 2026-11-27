# Stress relaxation during printing (T-601)

The warp model already estimates how much each layer shrinks as it cools below the profile's softening point. The added step estimates how much of that shrink still pulls on the part after the warm plastic has had time to relax. It multiplies the shrink strain by the fraction of stress remaining under fixed strain, evaluated at the layer's existing temperature estimate. The detailed run uses the warmer of the bed-and-air model and the layer heat calculation, as before.

The stress retention uses half a physical layer's printing time as a midpoint estimate for shrink that builds during that layer. It uses the loaded slice's layer times when available and the footprint-based time estimate otherwise. The solver uses this effective shrink for bed pull, peel, released curl, and residual stress. The number of elastic solves stays the same. This approximation treats each layer's temperature as constant during its brief relaxation window. It leaves out subsequent reheating, relaxation under later layers' loads, and cooling after printing.

## Material data

The tables in `core/fdmcore/data/thermal.yaml` interpolate in temperature and log(1 + seconds). Values beyond the table use the nearest endpoint. Each row starts at 100% stress at zero time. A user material inherits the data for its built-in base material when available.

- **PLA.** Kontou et al. tested compression-molded Ingeo 2003D. Figure 2b gives the relaxation modulus at 60 °C, and Section 4 gives the Williams–Landel–Ferry time shift (C1 = 19.4, C2 = 148.3 K). The table shifts approximate readings from the experimental curve, normalized to its short-time value of 3.2 GPa. At 10 s, the retained share is 99.4% at 25 °C and 80.6% at 40 °C. The colder rows depend on time–temperature superposition, and commercial filaments vary. [Kontou et al. 2024](https://doi.org/10.3390/nano14131116)
- **PETG.** Liu et al. tested PETG aligner sheet. Figure 7 gives the 37 °C master curve and Figure 8 gives the time shifts. The table uses approximate readings across the three PETG specimens, normalized to the early-time plateau. The hottest measured temperature was 53 °C, so this table holds that curve above 53 °C. Relaxation closer to a typical PETG filament's glass transition remains uncertain. [Liu et al. 2025](https://doi.org/10.4041/kjod25.094)
- **ABS.** Witek tested printed Prusa ABS at 25–100 °C with a 1.5% target tensile strain. The table integrates the fitted viscous branch (Equation 3 and Table 1) from the elastic stress at fixed strain. With total modulus K, viscous fraction f and viscous constants A and n, the normalized stress is `1 - f + f * (1 + (n - 1) * f * K * A * (0.015 * f * K) ** (n - 1) * t) ** (-1 / (n - 1))`. The transfer from this test strain to smaller print strains is approximate. [Witek 2023](https://doi.org/10.7494/cmms.2023.2.0813)

## What changed in the predictions

| PLA part | Previous curl (mm) | With relaxation (mm) | Physical comparison |
| --- | ---: | ---: | --- |
| Open-printer reference bar, 110 × 10 × 3 mm | 0.879 | 0.819 | The repo uses 110 mm as its estimated risk length. There is no recorded curl measurement for this bar. |
| Corpus tray, open printer | 0.744 | 0.726 | No recorded curl measurement for the tray. |
| Published 140 × 40 × 3.5 mm blocks | 0.851 | 0.720 | About 0.61 mm measured |
| Published 170 × 40 × 3.5 mm blocks | 1.009 | 0.843 | About 0.83 mm measured |

The first two rows use the Prusa MK4S PLA preset, with a 1 mm voxel for the reference bar and the default grid for the tray. The published comparison reconstructs the sharp and R5 footprints with 1.75 mm voxels and averages their four corner-to-center deviations after release. Budinoff et al. Figure 4 reports those averages from scans of parts printed on a LulzBot TAZ 5 at 0.2 mm layer height. The comparison uses 30 °C air from the paper's model inputs and retains the Prusa preset's 55 °C bed and other process settings where print settings are missing. The previous model overpredicts both of these blocks by about 0.24 and 0.18 mm. The updated estimates are high by about 0.11 and 0.01 mm, respectively. Direct print measurements would be needed to quantify the error on the project's own parts. [Budinoff, Sun and McMains 2020, Figure 4](https://doi.org/10.1115/MSEC2020-8526)

For the existing tall ABS peel fixture (90 × 10 × 12 mm, 1 mm voxels), relaxation reduces the fourth step's peak upward bed load from 8.46 to 8.25 N per node. The fourth step holds and peel starts on the fifth step at 5 mm. The previous model lifted at 4 mm. An open-printer ABS bar of 90 × 10 × 3 mm shows why the local stress and released curl can move in opposite directions. Its free curl rises from 1.198 to 1.221 mm as the bed-side layers lose more stress than the top. With relaxation, the 45 mm ABS bar still curls less in a preheated 60 °C chamber (0.203 mm) than in 25 °C air (0.359 mm).

The native warp solve for `core/tests/corpus/tray.stl` took a median of 6.68 s before and 6.64 s after, so relaxation adds no measurable time. The comparison times `warp.simulate` with the default grid (7,872 elements), bed release enabled as usual, and the optional peel solve off. It uses five timed runs after one warmup per version on a Ryzen 7 1700 with OpenBLAS capped at one thread. The baseline is `d0b5a06`.
