# CAD tricks, round 2 (T-181) — 2026-10-31

Gemini could not run in this session (its headless mode refuses tool use), so this round was a
short web search, every link opened and the claim found on the page.

## Added to the tool
- **Wall around a screw hole** (`screw_wall_thin`): a screw cut straight into the plastic needs about
  its own diameter of wall around the hole (M5: 5 mm). Hubs, *How to assemble 3D printed parts with
  threaded fasteners* — https://www.hubs.com/knowledge-base/how-assemble-3d-printed-parts-threaded-fasteners/
  ("the minimum wall thickness around a thread should match the diameter of the fastener").
  A tip, worded "if": pilot sizes overlap clearance holes (M4 pilot Ø3.4 = M3 clearance).
- **Brim ears** for long bases whose corners lift (OrcaSlicer wiki, Brim → Mouse ears:
  https://github.com/OrcaSlicer/OrcaSlicer/wiki/others_settings_brim).

## Found, not added
- **Fillet radius vs. stress in printed PLA**: measured stress concentration factors for fillets and holes —
  *Experimental Determination of Tensile Stress Concentration Factors of 3D-Printed PLA Polymer*,
  Advanced Materials Research 1180 (https://www.scientific.net/AMR.1180.13). Now a source for
  `stress_at_inside_corner`; its numbers could calibrate the fillet suggestion (needs the full paper).
- **Boss proportions** (injection-moulding practice, often borrowed for prints): boss height under ~3× its
  outside diameter, ribs/gussets to support tall bosses (HLH Rapid boss design guide). Not FDM-specific; no check.
- **Pilot hole sizes**: no authoritative FDM table found; our `pilot_mm` (~0.8 × d) stays as is. Printing a
  test boss is the reliable way (would fit the calibration prints, T-170).

## Not done (needs a working research agent or more time)
- Snap-fit cantilevers (strain limits along / across layers), living hinges (PP/TPU only), solid blocks →
  ribs/shells (Slant 3D shrink lines). Each needs its own detection code.
