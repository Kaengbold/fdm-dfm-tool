# Thermal constants: where the numbers come from

2026-11-18, for T-470. The values are in `core/fdmcore/data/thermal.yaml`. Gemini Flash drafted two tables (materials,
process); Claude checked every cited DOI against Crossref and read the open papers. **Gemini invented 5 of its 11
DOIs** (they point at unrelated papers or don't exist), so every value that leaned on them is now `estimated`.

## Checked sources

| Source | What we take from it | How it was checked |
|---|---|---|
| Seppala, Han, Hillgartner et al. 2017, *Weld formation during material extrusion AM*, Soft Matter 13:6761, doi:10.1039/c7sm00950j | ABS WLF: C1 = 4.65, C2 = 200.9 °C at Tref = 230 °C (shifted to Tg = 105 °C: 12.3 / 75.9 K). Makerbot ABS. **No reptation time**: the ABS shows no terminal viscosity. | Full text (PMC5684701) |
| Costa, Duarte, Covas 2014, *Thermal conditions affecting heat transfer in FDM/FFE*, Virtual Phys. Prototyping, doi:10.1080/17452759.2014.984042 | Convection 5-150 W/m²K, about 60 and above "most practical conditions"; emissivity 0.96; bead contact only bracketed (0 / 100 / 10⁶ W/m²K), not measured | Full text (UMinho repository) |
| Pyda, Bopp, Wunderlich 2004, *Heat capacity of poly(lactic acid)*, J. Chem. Thermodyn., doi:10.1016/j.jct.2004.05.003 | PLA c_p glassy and melt | DOI and title only; the numbers are the usual values from this paper, not re-read |
| Garlotta 2001, *A literature review of poly(lactic acid)*, J. Polym. Environ., doi:10.1023/A:1020200822435 | PLA ΔH for 100 % crystal, about 93 J/g | DOI and title |
| Pollard, Ward, Herrmann 2017, *Filament temperature dynamics in FDM*, Procedia Manuf., doi:10.1016/j.promfg.2017.07.147 | 12 °C and 18.5 °C temperature swings on feed-rate steps (abstract) | Abstract |
| Go & Hart 2017, *Fast desktop-scale extrusion AM*, Addit. Manuf., doi:10.1016/j.addma.2017.10.016 | Flow is limited by heating the filament. No hotend temperature-drop numbers | Full text (arXiv 1709.05918) |
| Coogan & Kazmer 2017, *Healing simulation for bond strength prediction of FDM*, Rapid Prototyping J., doi:10.1108/RPJ-03-2016-0051 | Method for T-473/T-474: integrate diffusion over the interface temperature history; predicted ABS bond strength with R² = 0.795. No constants taken | Abstract |

Rejected (fake or wrong DOI in Gemini's draft): "Costa 2014" doi:10.1108/RPJ-03-2013-0027 (an SLS paper), "Lepoivre 2020"
doi:10.1016/j.addma.2020.101488 (robocasting), "Brenken 2018" doi:10.1016/j.addma.2018.04.020 (tricalcium phosphate),
"Gilmer 2023" doi:10.1016/j.addma.2023.103522 (metal alloy), "Costa 2015" doi:10.1007/s00170-014-6330-y (does not exist).

## What is still weak

- **Reptation times and WLF for everything but ABS** are order-of-magnitude estimates. They drive the weld strength,
  so T-477's test prints should tune them first.
- **Fan heat transfer by fan %** is an estimated curve tied to one point. Fan hardware differs a lot between printers.
- **Contact conductances** (bead to bead, to the bed) are estimates; the one paper only brackets them.
- **TDS values** (density, Tg, Tm) match `presets.yaml` and the usual datasheets. The datasheets were not re-read one by one.
- Not found in open papers: melt temperature drop against flow for a standard hotend. An estimate is used, and T-482 can measure it.
# 2026-11-23 owner-supplied papers

Costa, Duarte and Covas, “Thermal conditions affecting heat transfer in FDM/FFE,” *Virtual and Physical Prototyping* 10 (2015), 35–46, online 2014, https://doi.org/10.1080/17452759.2014.984042. This is a numerical sensitivity study, not a measurement of printer coefficients. Journal page references follow.

| Model input or outcome | Number and context | Reference / uncertainty |
| --- | --- | --- |
| Case study | ABS-P400; extrusion 270 °C, environment 70 °C, speed 0.025 m/s, 0.3 mm circular filament; 0.1 s increment, 5% nominal contact area. Four-filament sequence 1 s per bead. No experimental sample count: simulations only. | pp. 37–40, Tables 1, 4, Fig. 7; uncertainty not quantified. ABS properties Tg 108 °C, conductivity 0.18 W/mK, heat capacity 2020 J/kgK, density 1050 kg/m³. |
| Convection | 5, 60, 150 W/m²K tested; 5 → 60 reduces time to Tg from 66 to 3 s; paper considers about 60 and above representative of most practical conditions. Rodriguez natural-convection calculation quoted as 67 W/m²K for a hot elliptical ABS filament, 270 °C into 70 °C environment. | pp. 38–39, Fig. 4. No fan percentage, fan curve, or calibrated still-air value measured. |
| Bead-to-bead gap conductance | 0, 100, and 10⁶ W/m²K (no, low, near-perfect contact) used as sensitivity cases; support 10 W/m²K, air h=60. A newly contacted cooler filament's central node rises 158.1 → 165.6 °C; surface node 167.8 → 192.8 °C in the illustrated case. | p. 40, Fig. 7. These are assumed conditions and simulated outcomes, not estimates with statistical uncertainty. |
| Support contact | 10 and 500 W/m²K tested with convection 60; 5% contact for circular vs 25% for square cross-section. Gap conductance and contact area both matter. | pp. 39–40, Fig. 6. Not measured PEI or glass interfaces. |
| Radiation and pockets | Emissivity 0.96; at h=5, including environmental radiation changes 15 s temperature by up to 10 °C; around h≥60 convection dominates. Inter-filament radiation changes <0.3 °C; trapped-air treatment <1.1 °C over 70–180 °C. | pp. 38–41, Figs. 5 and 7. Model-specific, not universal error bounds. |

**What to change in code (T-596):**

- `bead_contact_conductance` 500 [100, 5000] W/m²K → retain estimated central value and range, confidence low. Costa's 0/100/10⁶ cases do not calibrate 500 or constrain its range; investigate zero/near-perfect cases in a sensitivity study, not as a credible measured uncertainty interval. Contact area and conductance must be accounted for separately to avoid double counting when introducing bead necks.
- `h_still_air` 10 [5, 30] and `h_fan` 0/30/50/70/100% = 10/35/60/85/120 W/m²K → retain estimates, low confidence. Costa's 67 is natural convection for a hot, tiny bead, not a measured fan setting; 60 cannot validate the 50% anchor. Revise that source description to say it is a provisional scenario, not a fan calibration. Check whether environmental radiation is independently included at low h before using the paper's comparison.
- `bed_contact_conductance` smooth/textured PEI 300/150 [50, 1000] → retain estimates, low confidence; Costa's 10 and 500 are hypothetical support cases, with geometry-dependent area, not PEI measurements.
- `nozzle_reheat` 70 [30, 120] W/m²K, reach 1.5 [0.5, 3] mm → retain, low confidence; paper does not quantify nozzle radiation or reheating. No new value justified.
