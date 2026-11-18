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
