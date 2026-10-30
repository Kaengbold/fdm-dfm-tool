# Research prompt (for an AI with web access)

Copy everything below the line into ChatGPT Deep Research, Gemini Deep Research, Perplexity,
Claude with web search, or similar. Save the answer as `research/sourced_knowledge.md` and
open task T-020 in `TASKS.md`.

---

You are researching **design-for-FDM-3D-printing knowledge** for an open-source tool that
analyses a user's part (STL/STEP) and tells hobbyists what to change: print orientation,
slicer settings (OrcaSlicer / Bambu Studio / PrusaSlicer), and geometry. The tool detects
problems with code, then explains them from a knowledge base. I need that knowledge base
to be **correct, specific and sourced**.

## Sources

Search broadly and actually open the sources. Prefer, in this order:

1. **Measured tests**: CNC Kitchen (Stefan Hermann) strength tests and blog, Prusa research
   blog posts, university/lab papers on FDM anisotropy and interlayer strength, manufacturer
   technical data sheets (Bambu Lab, Prusament, Polymaker, eSUN) including Z-direction values.
2. **Official docs**: OrcaSlicer wiki (github.com/OrcaSlicer/OrcaSlicer/wiki and the in-app
   setting descriptions), Bambu Lab Wiki, Prusa Knowledge Base, Ultimaker/Cura settings guide.
3. **Respected practitioners**: Slant 3D (design-for-manufacturing videos and articles),
   Teaching Tech, Maker's Muse (Angus Deveson), Thomas Sanladerer, Made with Layers,
   3D Printing Nerd, Adam Savage's Tested only where specific, Hackaday articles.
4. **Community experience**: r/3Dprinting, r/FixMyPrint, r/prusa3d, r/BambuLab,
   r/functionalprint, Printables/MakerWorld comments, Prusa and Bambu forums — use these to
   find what beginners actually get wrong and which fixes work in practice; confirm numbers
   with (1)–(3) where possible.

For videos, give channel, title, URL and the timestamp of the claim.

## What to cover

For each topic, find the rule, the numbers, when it applies, and when it does NOT apply.

**A. Geometry / design rules**
- Overhang limits per material (PLA, PETG, ABS/ASA, TPU, PA, PA-CF, PC) and how cooling,
  layer height and line width change them.
- Bridging: max comfortable length per material, settings that extend it.
- Horizontal holes: teardrop vs diamond vs flat-top, sizes where it matters, hole shrinkage
  and compensation (polyholes, "precise wall", X-Y hole compensation).
- Counterbores / holes starting in mid-air: sacrificial layers, the "bridge stack" trick.
- Bed-contact edges: chamfer vs fillet, elephant-foot chamfer size, mouse ears.
- Minimum wall thickness and feature size per nozzle (0.2/0.4/0.6/0.8), Arachne effects.
- Text and embossing: minimum size, depth, font weight, top vs side orientation.
- Clearances: press fit, sliding fit, print-in-place hinges, per printer class.
- Snap fits, living hinges, cantilevers: what works in PLA vs PETG vs PA.
- Fasteners: heat-set insert hole sizes (M2–M5, brands), self-tapping screw pilot holes,
  nut traps, printed threads (when feasible).
- Ribs/gussets vs supports, splitting large parts, joining methods.
- Slant 3D's design rules (list them all, with their reasoning).

**B. Strength**
- Walls vs infill vs top/bottom layers: measured effect on strength (numbers).
- Interlayer (Z) strength as % of XY per material, and the effect of nozzle temperature,
  fan speed, layer height, line width, enclosure/chamber temperature.
- Infill patterns: measured strength/weight differences (gyroid, cubic, grid, adaptive, lightning).
- Annealing: which materials, shrinkage, gains.
- Fibre-filled materials: anisotropy and when they're worth it.

**C. OrcaSlicer features — when to use each** (use exact setting keys, e.g. `wall_loops`,
`sparse_infill_density`, `support_type`, `support_threshold_angle`, `brim_type`,
`wall_generator`, `elefant_foot_compensation`, `seam_position`, `ironing_type`,
`slow_down_for_layer_cooling`, `overhang_reverse`, `extra_perimeters_on_overhangs`,
`make_overhang_printable`, scarf seam settings, `precise_outer_wall`, `only_one_wall_top`,
`detect_thin_wall`, `bridge_flow`, `small_area_infill_flow_compensation`, …): what it does,
when it helps, when it hurts, typical values. Note differences in Bambu Studio / PrusaSlicer names.

**D. Problems → causes → fixes**: warping, corner lifting, elephant foot, stringing,
overhang curl, small-layer overheating, z-seam visibility, layer splitting, poor bed
adhesion, moisture (per material), heat creep, ringing. For each: how geometry or
orientation can prevent it, not just settings.

**E. Materials**: for PLA, PETG, ABS, ASA, TPU 95A, PA/PA-CF, PC: print/bed temps,
enclosure need, drying temp/time, abrasive or not, typical warping risk, fan settings,
XY and Z tensile strength with sources.

**F. Beginner mistakes**: the 30 most common design-for-printing mistakes hobbyists make
(ranked by how often they come up), each with the fix.

## Output format

Markdown. One section per topic letter. For every rule, use exactly this block:

```yaml
- id: short_snake_case_id
  title: max 60 characters, plain language
  applies_when: what a program could detect (geometry/orientation/material/setting), with thresholds
  why: 1–3 sentences a hobbyist understands
  fixes:
    - domain: geometry | slicer | process
      text: one actionable sentence
      settings: {orca_key: "value"}   # slicer fixes only, exact OrcaSlicer keys
  numbers: the key values with units and the range seen across sources
  exceptions: when this rule does not apply
  sources:
    - "Author/Channel — Title — URL — (timestamp or section) — what it supports"
  confidence: high (measured / official) | medium (expert consensus) | low (anecdotal)
```

Rules:
- Every number needs a source. If sources disagree, give the range and say which you trust and why.
- Mark claims you could not verify as `confidence: low` — do not drop them silently.
- No generic advice ("calibrate your printer") unless it's tied to a detectable condition.
- Quote the original wording when a source defines a setting.
- Finish with a list of sources you looked at but rejected, and why.
