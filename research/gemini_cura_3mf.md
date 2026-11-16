# Cura and 3MF settings (Gemini research, checked against Cura's source)

2026-11-16, for T-452. Drafted by Gemini Flash; every claim below was checked against Cura 5.9.0 and
libSavitar source. **Gemini's first answer was wrong on the main point** (it said a plain 3MF's settings
are ignored and only Cura project files carry them); the source says otherwise.

## Does Cura read per-model settings from a plain 3MF? Yes

- Cura's `plugins/3MFReader/ThreeMFReader.py` (lines ~153-190) takes `savitar_node.getSettings()` of each
  object and sets every key its per-object stack knows: `setting_container.setProperty(key, "value", value)`.
- libSavitar `src/SceneNode.cpp` (~lines 102-131) fills those settings from the object's
  `<metadatagroup><metadata name="...">value</metadata>`, stripping the `cura:` prefix (namespace
  `http://software.ultimaker.com/xml/cura/3mf/2015/10`, declared in `src/Namespace.cpp`; a bare `cura:` prefix
  also works). An older non-standard `<settings><setting key=...>` form is still read too.
- Cura's own 3MF writer writes exactly this (`plugins/3MFWriter/ThreeMFWriter.py`: `setSetting("cura:" + key, ...)`).

So a third-party 3MF only needs, on the `<object>` (3MF core: `metadatagroup` before `mesh`):

```xml
<model ... xmlns:cura="http://software.ultimaker.com/xml/cura/3mf/2015/10">
  <object id="1" type="model">
   <metadatagroup>
    <metadata name="cura:wall_line_count" preserve="1">4</metadata>
   </metadatagroup>
   <mesh>...</mesh>
```

No Cura project file is needed (a project file adds `Cura/*.cfg` stacks and asks "open as project";
it needs the user's exact printer definition, so it is the worse route for us).

## Setting names (checked in `resources/definitions/fdmprinter.def.json`, Cura 5.9.0)

| Ours (OrcaSlicer key) | Cura key | Per model? | Conversion |
|---|---|---|---|
| wall_loops | wall_line_count | yes | same |
| sparse_infill_density | infill_sparse_density | yes | "25%" → 25 |
| sparse_infill_pattern | infill_pattern | yes | gyroid, grid, lightning: same names |
| top_shell_layers / bottom_shell_layers | top_layers / bottom_layers | yes | same |
| enable_support | support_enable | yes | 1/0 → True/False |
| support_threshold_angle | support_angle | yes | Orca from horizontal, Cura from vertical: 90 − value |
| xy_hole_compensation | hole_xy_offset | yes | same sign (positive = bigger hole) |
| xy_contour_compensation | xy_offset | yes | same sign |
| elefant_foot_compensation | xy_offset_layer_0 | yes | negated (Cura pulls in with a negative value) |
| ironing_type | ironing_enabled (+ ironing_only_highest_layer for "topmost") | yes | |
| bridge_speed | bridge_settings_enabled + bridge_wall_speed + bridge_skin_speed | yes | |
| fuzzy_skin (+ thickness, point distance) | magic_fuzzy_skin_enabled, _outside_only, _thickness, _point_dist | yes | |
| layer_height | layer_height | **no** (global) | user sets it |
| support_type tree | support_structure = tree | **no** | user sets it |
| brim_type / brim_width | adhesion_type / brim_width | **no** | user sets it |
| slow_down_for_layer_cooling | cool_min_layer_time | **no** | user sets it |
| wall_generator arachne | — | — | Cura 5 always uses Arachne; nothing to set |

Gemini's errors fixed here: it named `initial_layer_xy_offset` (doesn't exist; it is `xy_offset_layer_0`),
guessed a key for Arachne (there is none), and marked support angle per-model as unsure (it is per-model).

## Not verified

Cura itself wasn't run (not installed on the test machine): the export test reads the file the way the
reader above does. Worth one real check in Cura when someone has it (T-467).

## Sources

- https://github.com/Ultimaker/Cura/blob/5.9.0/plugins/3MFReader/ThreeMFReader.py
- https://github.com/Ultimaker/Cura/blob/5.9.0/plugins/3MFWriter/ThreeMFWriter.py
- https://github.com/Ultimaker/Cura/blob/5.9.0/resources/definitions/fdmprinter.def.json
- https://github.com/Ultimaker/libSavitar/blob/main/src/SceneNode.cpp
- https://github.com/Ultimaker/libSavitar/blob/main/src/Namespace.cpp
