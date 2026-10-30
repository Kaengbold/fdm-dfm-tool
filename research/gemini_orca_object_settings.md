# OrcaSlicer Per-Object vs Per-Plate Settings Architecture (T-136)

## Source Scope Partitioning (`src/libslic3r/PrintConfig.cpp`)

In the OrcaSlicer source (`src/libslic3r/PrintConfig.cpp`), print settings are divided into distinct configuration classes that govern serialization into `Metadata/model_settings.config` in project 3MF files:

### 1. Per-Object Configuration (`PrintObjectConfig`)
Settings initialized inside `PrintObjectConfig::PrintObjectConfig()` apply to an individual model object:
- `layer_height`
- `elefant_foot_compensation`
- `xy_hole_compensation`
- `xy_contour_compensation`
- `fuzzy_skin`
- `fuzzy_skin_point_distance`
- `fuzzy_skin_thickness`
- `fuzzy_skin_mode`
- `brim_type`
- `brim_width`
- `brim_ears`
- `support_material`
- `support_material_auto`
- `support_type`
- `seam_position`
- `raft_layers`

### 2. Per-Region / Per-Modifier Configuration (`PrintRegionConfig`)
Settings initialized inside `PrintRegionConfig::PrintRegionConfig()` apply per object and per modifier volume / height-range modifier:
- `wall_loops`
- `sparse_infill_density`
- `sparse_infill_pattern`
- `top_shell_layers`
- `bottom_shell_layers`
- `top_surface_pattern`
- `bottom_surface_pattern`
- `ironing_type`
- `ironing_speed`
- `ironing_flowrate`
- `small_perimeter_speed`
- `overhang_speed`
- `bridge_speed`
- `disable_bridge_infill`

### 3. Print / Plate Only Configuration (`PrintConfig`)
Settings that only apply globally or per plate:
- `print_sequence` (by layer vs by object)
- `filament_diameter`
- `nozzle_diameter`
- `bed_temperature`
- Machine G-code templates and extruder mapping

### Source Reference
- Repository: https://github.com/OrcaSlicer/OrcaSlicer/blob/main/src/libslic3r/PrintConfig.cpp
- Header definitions: https://github.com/OrcaSlicer/OrcaSlicer/blob/main/src/libslic3r/PrintConfig.hpp