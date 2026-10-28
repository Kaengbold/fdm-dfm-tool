# Research

| File | What | Status |
|---|---|---|
| `RESEARCH_PROMPT.md` | Prompt for an AI with web access | — |
| `sourced_A1_geometry_rules.yaml` | Overhangs, bridges, holes, chamfers, walls, text | sourced (links checked) |
| `sourced_A2_slant3d_design_rules.yaml` | Slant 3D design-for-manufacturing rules | sourced |
| `sourced_B_strength.yaml` | Walls vs infill, layer strength | sourced |
| `sourced_C_orcaslicer_settings.yaml` | OrcaSlicer settings and when to use them | sourced |
| `sourced_D_print_problems.yaml` | Warping, elephant foot, stringing, … | sourced |
| `sourced_E_materials.yaml` | PLA, PETG, ABS/ASA, TPU, PA, PC | sourced |
| `sourced_F_beginner_mistakes.yaml` | 30 common beginner mistakes | sourced |
| `url_check.txt` | HTTP status of every source link (64/82 OK on 2026-10-28) | — |
| `thresholds_extracted.yaml` | Numbers pulled from the sourced files (by Gemini) → `core/fdmcore/data/thresholds.yaml` | used |
| `kb_new_rules_draft.yaml` | Gemini draft of 12 rules, reviewed and merged into `rules.yaml` | merged |
| `fdm_knowledge.md` | Early overview from model knowledge, no live sources | unverified |

YAML from LLMs often has quoting errors: `python tools/fix_research_yaml.py research/*.yaml`.
