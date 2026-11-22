<!-- Paste into the AGENTS.md of a project where Codex (or another agent) should check parts for 3D printing. -->

## 3D printing checks (fdm-dfm)

Parts to print go through the `fdm-dfm` MCP server (or the `fdmdfm` command line, JSON out).
Before using it, read the guide: `<path to fdm-dfm-tool>/agents/fdm-check/SKILL.md`. In short:

- Flow: `list_presets` → `load_part` → loads only if the part carries something (`list_faces`) →
  `optimize_print(goal, loads)` (goal strong / balanced / fast / light / quality: 2–4 checked options, each with
  `up` and `settings`) → `check_print(up)` → `export_3mf(up, path, settings=…)` → `write_report` (HTML for the user).
- Units: mm, kg (weight carried), °C, infill 0–1. Safety targets by goal: ×5 / ×3 / ×2 / ×1.5 (quality: no strength).
- Results are rough estimates. Explain them in plain words for hobbyists, lead with the answer, give a fix.
