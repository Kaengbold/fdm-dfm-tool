# Quality settings: what makes a print look good (T-572)

The Quality goal (plan §14) skips strength and optimises for looks. This note collects which slicer settings make a
visible surface better, how sure we are, and what the app does with each. The values live in
`core/fdmcore/data/thresholds.yaml` (`looks:`) and are applied in `recommend.py` when the profile says `looks`
(the new UI sends it for the Quality goal).

Rule we follow: a setting goes in only when a slicer's own documentation or its own shipped profiles back it, and only
in a way that can't make a print worse on a printer we don't know (e.g. never faster than the printer's own speed).

## Settings the Quality goal sets

| Setting (OrcaSlicer key) | Value | Why | Source |
|---|---|---|---|
| Outer wall speed (`outer_wall_speed`) | 0.3 × the printer's usual outer-wall speed (fast printers 200 → 60 mm/s; medium 140 → 42 mm/s; other nozzles scaled as their own profiles) | Slower outer walls give a smoother surface with less ringing. | OrcaSlicer 2.5.0's Bambu **High Quality** process profiles: outer wall 60 mm/s where **Standard** uses 200 (0.12 / 0.16 mm, X1C; pinned in `core/fdmcore/data/orca_profiles.json.gz`). Creality wiki, speed: slow outer walls reduce vibration and ringing. Ultimaker support: slower outer walls usually improve the finish. |
| Outer wall acceleration (`outer_wall_acceleration`) | 2000 mm/s², fast printers only | Gentler starts and stops on the outer wall: less ringing next to corners. A "medium" printer's own default is often lower already, so it isn't touched there. | Same High Quality profiles: 2000 against Standard's 5000. OrcaSlicer wiki, speed_settings_acceleration: the outer wall is usually set lower than normal printing for quality. |
| Wall order (`wall_sequence`) | `inner-outer-inner wall` | The outer wall prints between two inner ones, undisturbed: the best outer surface and dimensional accuracy. | OrcaSlicer wiki, quality_settings_wall_and_surfaces (recommends Inner/Outer/Inner for surface finish; Inner/Outer for overhangs; Outer/Inner makes seams less consistent). |
| Seam (`seam_position`) | `aligned` | Seams line up in one place instead of jumping between corners. | OrcaSlicer wiki, quality_settings_seam: Aligned, Aligned Back or Back typically work best. `aligned` is the value OrcaSlicer, Bambu Studio and PrusaSlicer all know (Orca's `back` is Prusa's `rear`). The scarf seam for round outlines was already in (`seam_slope_type`, thresholds `seam:`). |
| Ironing (`ironing_type`) | `top` (every flat top) on parts with a large flat top | For looks the lower flat tops matter too; elsewhere it stays `topmost` (time). | OrcaSlicer wiki, quality_settings_ironing; Prusa KB ironing_177488 (both already cited in thresholds `ironing:`). |
| Ironing flow / spacing (`ironing_flow`, `ironing_spacing`) | 10 % / 0.1 mm, or the user's flow from the ironing test | Common starting points; the right flow depends on the filament and printer, hence the test. | OrcaSlicer's shipped profiles: Creality K2 10 % / 0.15 mm, Bambu 8 %. Guides: Printago (~10 %, 0.1 mm, 15–30 mm/s), Stacksheriff (PLA 10–15 %, PETG 8–12 %, 0.08–0.12 mm), Sovol (8–12 %, 0.08–0.15 mm). The wiki defines the settings but gives no values. |

## Looked at, not set

- **Bridges** (speed, fan, flow): sources disagree (the OrcaSlicer wiki suggests a bridge flow ratio a little under 1,
  a forum report finds the opposite; a fan setting can't go below the layer's own fan in Orca). No value transfers
  between printers. The app keeps its targeted change: a slower bridge (25 mm/s) only where a check finds a bridge
  that sags.
- **Cooling** (fan): the material's slicer profile already runs the fan high for PLA; per-height cooling is the heat
  model's job (Tuned settings, T-571 / T-479), not a blanket value.
- **Inner wall speed**: High Quality halves it (150 vs 300), and slower inner walls show less on the outside, but the
  speed table has no inner-wall class per printer yet; left out rather than guessed.

## Checked in the slicers (installed OrcaSlicer, Bambu Studio, PrusaSlicer, 2026-11-22)

Each 3MF was sliced with the slicer's command line (`tools/slicer_check.py`'s `slice_with`) and the G-code read back:

- Looks settings on a test part: OrcaSlicer and Bambu Studio print inner, outer, inner wall at mid-height; the outer
  wall runs at 60 mm/s in all three; OrcaSlicer uses 2000 mm/s² on it (Bambu Studio used its own, lower 500;
  PrusaSlicer has no per-object outer-wall acceleration, so it isn't mapped).
- Ironing test (5 steps, flows 18 / 15 / 12 / 9 / 6 % set as height ranges): the median ironing extrusion per mm on the
  five tops is 0.00150 / 0.00125 / 0.00100 / 0.00075 / 0.00050 mm in **all three** slicers, exactly the set ratios.
- Temperature tower: the pass that writes each block's temperature into the user's sliced G-code puts the change on the
  first layer of each block in all three slicers' G-code, and leaves the end code's heater-off alone.

## Calibration tests (Settings, Calibration tests)

- **Temperature tower**: six 8 mm blocks on a base, each with a 45° overhang, its temperature written on its front;
  the material's usual temperature +10 … −15 °C in 5 °C steps, hottest at the bottom (the usual tower layout; the 5 °C
  step is what OrcaSlicer's own calibration uses). The user slices it, gets the G-code back with the temperatures
  set (`api.tuning_gcode("temp")`, `gcode.add_temperatures`), prints it and picks the best block: saved as the
  material's `nozzle_temp_c` (the heat simulations use it). OrcaSlicer and Bambu Studio can also make their own tower.
- **Ironing**: five steps, one flow each (6–18 %), as a 3MF with the flows already set per height. The smoothest top
  is saved as `ironing_flow_pct` and used whenever a top is ironed.

## Sources

- OrcaSlicer wiki, Wall and surfaces: https://www.orcaslicer.com/wiki/print_settings/quality/quality_settings_wall_and_surfaces
- OrcaSlicer wiki, Seam: https://www.orcaslicer.com/wiki/print_settings/quality/quality_settings_seam
- OrcaSlicer wiki, Ironing: https://www.orcaslicer.com/wiki/print_settings/quality/quality_settings_ironing
- OrcaSlicer wiki, Bridging: https://www.orcaslicer.com/wiki/print_settings/quality/quality_settings_bridging
- OrcaSlicer wiki, Acceleration: https://github.com/OrcaSlicer/OrcaSlicer/wiki/speed_settings_acceleration
- OrcaSlicer wiki, Calibration guide (temperature tower): https://www.orcaslicer.com/wiki/guides/calibration_guide
- OrcaSlicer 2.5.0 process profiles (Bambu High Quality / Standard, Creality K2), pinned in `core/fdmcore/data/orca_profiles.json.gz` (https://github.com/OrcaSlicer/OrcaSlicer)
- Bambu Lab wiki, Quality advance settings (wall order and overhangs): https://wiki.bambulab.com/en/software/bambu-studio/parameter/quality-advance-settings
- Prusa KB, Seam position: https://help.prusa3d.com/article/seam-position_151069
- Prusa KB, Poor bridging: https://help.prusa3d.com/article/bridging_1802
- Creality wiki, Speed parameters: https://wiki.creality.com/en/software/creality-print/parameter-speed
- Ultimaker support, surface quality: https://support.ultimaker.com/articles/Knowledge/1667411313568
- Printago, ironing settings: https://printago.io/guides/orca-slicer-ironing
- Stacksheriff, OrcaSlicer ironing settings: https://stacksheriff.com/3d-printing/orcaslicer-ironing-settings-explained/
- Sovol, ironing settings: https://www.sovol3d.com/blogs/news/ironing-in-3d-printing-settings-for-smoother-top-surfaces
- Obico, OrcaSlicer calibration guide (5 °C tower steps): https://www.obico.io/blog/orcaslicer-comprehensive-calibration-guide/
