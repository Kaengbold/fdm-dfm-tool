"""3MF export of the oriented part, with OrcaSlicer/Bambu Studio per-object settings.

The geometry part is plain 3MF core spec and opens in any slicer. The settings are
written as per-object overrides twice: ``Metadata/model_settings.config`` (OrcaSlicer and
Bambu Studio keys) and ``Metadata/Slic3r_PE_model.config`` (PrusaSlicer names, see
PRUSA_KEYS). Each slicer reads its own file. tools/slicer_check.py checks that all three apply them.

Modifier volumes (zones printed with more walls / infill) need a different file per slicer:
OrcaSlicer and Bambu Studio only read them as separate component objects, PrusaSlicer only as
triangle ranges of one mesh (each merges the other layout into the part). Without modifiers one
file serves all three.

Seam painting (keep the seam off faces the user can see) is stored per triangle, under each slicer's
own name: ``paint_seam`` (OrcaSlicer, Bambu Studio) and ``slic3rpe:custom_seam`` (PrusaSlicer). Both
hold the slicers' painted-triangle code; a whole triangle painted as "blocker" is "8".
"""

from __future__ import annotations

import io
import zipfile
from xml.sax.saxutils import quoteattr

import numpy as np

from .mesh import Mesh

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
 <Default Extension="config" ContentType="text/xml"/>
</Types>"""

RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>"""


# OrcaSlicer key -> PrusaSlicer key (values mean the same). Keys without a per-object
# PrusaSlicer equivalent (cooling, hole compensation) are left out there.
PRUSA_KEYS = {
    "layer_height": "layer_height",
    "wall_loops": "perimeters",
    "sparse_infill_density": "fill_density",
    "sparse_infill_pattern": "fill_pattern",
    "top_shell_layers": "top_solid_layers",
    "bottom_shell_layers": "bottom_solid_layers",
    "enable_support": "support_material",
    "support_threshold_angle": "support_material_threshold",
    "support_on_build_plate_only": "support_material_buildplate_only",
    "brim_type": "brim_type",
    "brim_width": "brim_width",
    "raft_layers": "raft_layers",
    "wall_generator": "perimeter_generator",
    "bridge_speed": "bridge_speed",
    "fuzzy_skin": "fuzzy_skin",  # none | external | all in both
    "fuzzy_skin_thickness": "fuzzy_skin_thickness",
    "fuzzy_skin_point_distance": "fuzzy_skin_point_dist",
    "seam_position": "seam_position",
    "ironing_flow": "ironing_flowrate",  # a percent in both
    "ironing_spacing": "ironing_spacing",  # nearest | aligned | random in both (Orca's back is Prusa's rear: not used)
    # speeds (mm/s), also per height range (T-479)
    "outer_wall_speed": "external_perimeter_speed",
    "inner_wall_speed": "perimeter_speed",
    "sparse_infill_speed": "infill_speed",
    "internal_solid_infill_speed": "solid_infill_speed",
    "top_surface_speed": "top_solid_infill_speed",
}


def prusa_settings(settings: dict[str, str]) -> dict[str, str]:
    out = {PRUSA_KEYS[k]: str(v) for k, v in settings.items() if k in PRUSA_KEYS}
    if settings.get("support_type", "").startswith("tree"):
        out["support_material_style"] = "organic"
    if settings.get("ironing_type", "no ironing") != "no ironing":
        out["ironing"] = "1"
        out["ironing_type"] = settings["ironing_type"]  # same values: top | topmost | solid
    if out.get("support_material") == "1":
        out["support_material_auto"] = "0" if settings.get("support_type", "").endswith("(manual)") else "1"
    if out.get("brim_type") == "brim_ears":  # PrusaSlicer has no mouse ears
        out["brim_type"] = "outer_only"
    return out


CURA_NS = "http://software.ultimaker.com/xml/cura/3mf/2015/10"
# OrcaSlicer key -> Cura key, for the settings Cura takes per model (``settable_per_mesh`` in Cura's
# fdmprinter.def.json, 5.9). Cura reads them from ``cura:`` metadata on the object of a plain 3MF
# (plugins/3MFReader/ThreeMFReader.py with libSavitar). Layer height, support type, brim and minimum
# layer time are global in Cura: CURA_GLOBAL lists them for the user to set by hand.
CURA_KEYS = {
    "wall_loops": "wall_line_count",
    "top_shell_layers": "top_layers",
    "bottom_shell_layers": "bottom_layers",
    "xy_hole_compensation": "hole_xy_offset",
    "xy_contour_compensation": "xy_offset",
}
CURA_GLOBAL = {"layer_height": "Layer Height", "support_type": "Support Structure", "brim_type": "Build Plate Adhesion Type",
               "brim_width": "Brim Width", "raft_layers": "Build Plate Adhesion Type (Raft)", "slow_down_for_layer_cooling": "Minimum Layer Time"}
CURA_PATTERNS = {"gyroid", "grid", "lightning"}


def cura_settings(settings: dict[str, str]) -> dict[str, str]:
    """The per-model part of ``settings`` under Cura's names and units."""
    out = {CURA_KEYS[k]: str(v) for k, v in settings.items() if k in CURA_KEYS}
    if "sparse_infill_density" in settings:
        out["infill_sparse_density"] = str(settings["sparse_infill_density"]).rstrip("%")
    if settings.get("sparse_infill_pattern") in CURA_PATTERNS:
        out["infill_pattern"] = settings["sparse_infill_pattern"]
    if "enable_support" in settings:
        out["support_enable"] = "True" if str(settings["enable_support"]) == "1" else "False"
    if "support_threshold_angle" in settings:  # OrcaSlicer measures from the horizontal, Cura from the vertical
        out["support_angle"] = f"{90 - float(settings['support_threshold_angle']):g}"
    if "elefant_foot_compensation" in settings:  # Cura pulls the first layer in with a negative expansion
        out["xy_offset_layer_0"] = f"{-float(settings['elefant_foot_compensation']):g}"
    if settings.get("ironing_type", "no ironing") != "no ironing":
        out["ironing_enabled"] = "True"
        out["ironing_only_highest_layer"] = "True" if settings["ironing_type"] == "topmost" else "False"
    if "bridge_speed" in settings:
        out.update(bridge_settings_enabled="True", bridge_wall_speed=str(settings["bridge_speed"]),
                   bridge_skin_speed=str(settings["bridge_speed"]))
    if settings.get("fuzzy_skin", "none") != "none":
        out["magic_fuzzy_skin_enabled"] = "True"
        out["magic_fuzzy_skin_outside_only"] = "True" if settings["fuzzy_skin"] == "external" else "False"
        for ours, theirs in (("fuzzy_skin_thickness", "magic_fuzzy_skin_thickness"), ("fuzzy_skin_point_distance", "magic_fuzzy_skin_point_dist")):
            if ours in settings:
                out[theirs] = str(settings[ours])
    return out


def _cura_meta(settings: dict[str, str]) -> str:
    """Cura's per-model settings as the object's metadata group (3MF core spec: before the mesh)."""
    rows = "\n".join(f'    <metadata name={quoteattr("cura:" + k)} preserve="1">{v}</metadata>' for k, v in cura_settings(settings).items())
    return f"\n   <metadatagroup>\n{rows}\n   </metadatagroup>" if rows else ""


LAYOUTS = ("orca", "prusa")
# Extra volumes in the part: OrcaSlicer / Bambu Studio part subtype, PrusaSlicer volume_type, label.
VOLUME_TYPES = {
    "stronger": ("modifier_part", "ParameterModifier", "zone"),
    "support": ("support_enforcer", "SupportEnforcer", "support here"),
    "blocker": ("support_blocker", "SupportBlocker", "no support here"),
    "void": ("negative_part", "NegativeVolume", "hidden gap"),
}


def export_3mf(mesh: Mesh, name: str = "part", settings: dict[str, str] | None = None, bed_center=(128.0, 128.0),
               modifiers: list[tuple] = (), layout: str = "orca", pauses: list[dict] = (), ranges: list[dict] = (),
               seam_block: np.ndarray | None = None, copies=((0.0, 0.0),), layer_height: float | None = None) -> bytes:
    """``mesh`` must already be oriented (lowest point at z = 0, centred on x/y = 0). ``modifiers``:
    (triangles in the same frame, settings[, kind]) per volume, kind one of VOLUME_TYPES (default
    "stronger": a settings modifier); ``layout`` "orca" (also Bambu Studio) or "prusa", see the
    module docstring. ``pauses``: {"top": pocket top (mm), "layer_height", "note"} each: pause before the
    first layer above it, to put something into the part (see pause_files). ``ranges``: {"z0", "z1",
    key: value, ...} each: other settings over that height (see range_files; ``settings``' layer height,
    else ``layer_height``, where a range sets none). ``seam_block``: one bool per
    triangle of ``mesh``: no seam there (painted as seam blocker). ``copies``: (x, y) offset of each copy on the
    plate, as build items of the one object (all three slicers print each; OrcaSlicer and Bambu Studio may
    re-arrange them)."""
    layer = (settings or {}).get("layer_height", layer_height)
    extra = {**pause_files(pauses), **range_files(list(ranges), None if layer is None else float(layer))}
    if modifiers:
        if layout not in LAYOUTS:
            raise ValueError(f"layout must be one of {LAYOUTS}")
        return _with_modifiers(mesh, name, settings or {}, bed_center, modifiers, layout, extra, seam_block, copies)
    verts, faces, paint = _weld_painted(mesh.triangles, seam_block)
    verts = verts + [bed_center[0], bed_center[1], 0.0]
    vxml = "\n".join(f'<vertex x="{x:.4f}" y="{y:.4f}" z="{z:.4f}"/>' for x, y, z in verts)
    txml = _triangles_xml(faces, paint)
    model = f"""<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" xmlns:slic3rpe="http://schemas.slic3r.org/3mf/2017/06" xmlns:cura="{CURA_NS}">
 <metadata name="Application">FDM DFM Tool</metadata>
 <resources>
  <object id="1" name={quoteattr(name)} type="model">{_cura_meta(settings or {})}
   <mesh>
    <vertices>
{vxml}
    </vertices>
    <triangles>
{txml}
    </triangles>
   </mesh>
  </object>
 </resources>
 <build>
{_items(copies)}
 </build>
</model>"""
    meta = "\n".join(f"    <metadata key={quoteattr(k)} value={quoteattr(str(v))}/>" for k, v in (settings or {}).items())
    model_settings = f"""<?xml version="1.0" encoding="UTF-8"?>
<config>
  <object id="1">
    <metadata key="name" value={quoteattr(name)}/>
{meta}
  </object>
</config>"""
    prusa_meta = "\n".join(f"  <metadata type=\"object\" key={quoteattr(k)} value={quoteattr(v)}/>"
                           for k, v in prusa_settings(settings or {}).items())
    prusa_model = f"""<?xml version="1.0" encoding="UTF-8"?>
<config>
 <object id="1" instances_count="{len(copies)}">
  <metadata type="object" key="name" value={quoteattr(name)}/>
{prusa_meta}
  <volume firstid="0" lastid="{len(faces) - 1}">
   <metadata type="volume" key="name" value={quoteattr(name)}/>
   <metadata type="volume" key="volume_type" value="ModelPart"/>
  </volume>
 </object>
</config>"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        z.writestr("3D/3dmodel.model", model)
        z.writestr("Metadata/model_settings.config", model_settings)
        z.writestr("Metadata/Slic3r_PE_model.config", prusa_model)
        for path, text in extra.items():
            z.writestr(path, text)
    return buf.getvalue()


def range_files(ranges: list[dict], layer_height: float | None = None) -> dict[str, str]:
    """Height range modifiers: other settings between two heights (``{"z0", "z1", key: value, ...}``, OrcaSlicer
    keys: thinner layers, slower speeds), as the slicers store them in their project files: OrcaSlicer /
    Bambu Studio ``layer_config_ranges.xml``, PrusaSlicer ``Prusa_Slicer_layer_config_ranges.xml`` (same
    layout, its own keys). The slicers take no overlapping ranges, so ranges are cut where they meet and
    each piece gets the settings of every range over it (the smaller value where two set the same key:
    the thinner layer, the slower speed). Every range carries a layer height (``layer_height`` when it
    sets none). Percent values ("12%") stay percent. tools/slicer_check.py checks that all three apply them."""
    pieces = []
    pct = {k for r in ranges for k, v in r.items() if isinstance(v, str) and v.strip().endswith("%")}
    cuts = sorted({float(r["z0"]) for r in ranges} | {float(r["z1"]) for r in ranges})
    for lo, hi in zip(cuts, cuts[1:]):
        opts: dict[str, float] = {}
        for r in ranges:
            if r["z0"] <= lo + 1e-9 and r["z1"] >= hi - 1e-9:
                for k, v in r.items():
                    if k not in ("z0", "z1"):
                        opts[k] = min(float(str(v).strip().rstrip("%")), opts.get(k, float("inf")))
        if not opts:
            continue
        if "layer_height" not in opts:
            if layer_height is None:
                raise ValueError("A height range needs a layer height")
            opts["layer_height"] = float(layer_height)
        if pieces and pieces[-1][1] == lo and pieces[-1][2] == opts:
            pieces[-1] = (pieces[-1][0], hi, opts)  # the same settings go on: one range
        else:
            pieces.append((lo, hi, opts))
    if not pieces:
        return {}

    def text(keys) -> str:
        body = ""
        for lo, hi, opts in pieces:
            lines = "".join(f'  <option opt_key="{keys[k]}">{v:.3f}</option>\n' if k == "layer_height" else f'  <option opt_key="{keys[k]}">{v:g}{"%" if k in pct else ""}</option>\n'
                            for k, v in opts.items() if k in keys)
            body += f' <range min_z="{lo:.3f}" max_z="{hi:.3f}">\n  <option opt_key="extruder">0</option>\n{lines} </range>\n'
        return f'<?xml version="1.0" encoding="utf-8"?>\n<objects>\n<object id="1">\n{body}</object>\n</objects>\n'

    keys = {k for _, _, o in pieces for k in o}
    return {"Metadata/layer_config_ranges.xml": text({k: k for k in keys}), "Metadata/Prusa_Slicer_layer_config_ranges.xml": text(PRUSA_KEYS)}


def pause_files(pauses: list[dict]) -> dict[str, str]:
    """The slicers' own "pause at this layer" lists, as their project files store them: OrcaSlicer and
    Bambu Studio read ``custom_gcode_per_layer.xml``, PrusaSlicer its ``custom_gcode_per_print_z.xml``.
    type 1 is a pause (each uses the printer profile's own pause command, e.g. M601 / M400 U1). The pause
    must come before the first layer over the pocket's ``top``, whatever the layer grid: PrusaSlicer pauses
    at the next layer at or above the height given (so: just above the top), OrcaSlicer at the nearest
    one (so: most of a layer above it). tools/slicer_check.py checks both."""
    if not pauses:
        return {}
    orca = "".join(f'<layer top_z="{p["top"] + 0.6 * p["layer_height"]:.3f}" type="1" extruder="1" color="" extra={quoteattr(p.get("note", ""))} gcode="M601"/>\n'
                   for p in pauses)
    prusa = "".join(f'<code print_z="{p["top"] + 0.01:.3f}" type="1" extruder="1" color="" extra={quoteattr(p.get("note", ""))} gcode="M601"/>\n'
                    for p in pauses)
    return {
        "Metadata/custom_gcode_per_layer.xml": ('<?xml version="1.0" encoding="utf-8"?>\n<custom_gcodes_per_layer>\n<plate>\n'
                                                f'<plate_info id="1"/>\n{orca}<mode value="SingleExtruder"/>\n</plate>\n</custom_gcodes_per_layer>\n'),
        "Metadata/Prusa_Slicer_custom_gcode_per_print_z.xml": ('<?xml version="1.0" encoding="utf-8"?>\n<custom_gcodes_per_print_z>\n'
                                                               f'{prusa}<mode value="SingleExtruder"/>\n</custom_gcodes_per_print_z>\n'),
    }


MODEL_HEAD = """<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" xmlns:slic3rpe="http://schemas.slic3r.org/3mf/2017/06">
 <metadata name="Application">FDM DFM Tool</metadata>
 <resources>
"""
MODEL_FOOT = """ </resources>
 <build>
{items}
 </build>
</model>"""


def _items(copies) -> str:
    """Build items: one per copy of object 1, moved by its (x, y) offset."""
    return "\n".join(f'  <item objectid="1" transform="1 0 0 0 1 0 0 0 1 {x:.3f} {y:.3f} 0" printable="1"/>' if (x, y) != (0, 0)
                     else '  <item objectid="1" printable="1"/>' for x, y in copies)


SEAM_BLOCK = ' paint_seam="8" slic3rpe:custom_seam="8"'  # whole triangle painted "blocker", both slicers' names


def _triangles_xml(faces: np.ndarray, paint: np.ndarray | None = None) -> str:
    """Triangle lines; ``paint`` (one bool per face): painted as seam blocker."""
    if paint is None or not paint.any():
        return "\n".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in faces)
    return "\n".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"{SEAM_BLOCK if p else ""}/>' for (a, b, c), p in zip(faces, paint))


def _mesh_xml(verts: np.ndarray, faces: np.ndarray, paint: np.ndarray | None = None) -> str:
    vxml = "\n".join(f'<vertex x="{x:.4f}" y="{y:.4f}" z="{z:.4f}"/>' for x, y, z in verts)
    return f"<mesh>\n<vertices>\n{vxml}\n</vertices>\n<triangles>\n{_triangles_xml(faces, paint)}\n</triangles>\n</mesh>"


def _meta(d: dict, kind: str | None = None) -> str:
    """Metadata lines; ``kind`` "object" / "volume" for PrusaSlicer's config."""
    t = f'type="{kind}" ' if kind else ""
    return "\n".join(f"    <metadata {t}key={quoteattr(k)} value={quoteattr(str(v))}/>" for k, v in d.items())


def _zip(model: str, model_settings: str | None, prusa_model: str | None, extra: dict[str, str] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        z.writestr("3D/3dmodel.model", model)
        if model_settings:
            z.writestr("Metadata/model_settings.config", model_settings)
        if prusa_model:
            z.writestr("Metadata/Slic3r_PE_model.config", prusa_model)
        for path, text in (extra or {}).items():
            z.writestr(path, text)
    return buf.getvalue()


def _with_modifiers(mesh: Mesh, name: str, settings: dict, bed_center, modifiers, layout: str, extra: dict[str, str] | None = None,
                    seam_block: np.ndarray | None = None, copies=((0.0, 0.0),)) -> bytes:
    foot = MODEL_FOOT.format(items=_items(copies))
    shift = np.array([bed_center[0], bed_center[1], 0.0])
    v0, f0, paint = _weld_painted(mesh.triangles, seam_block)
    pieces = [(v0, f0)] + [_weld(m[0]) for m in modifiers]
    types = [VOLUME_TYPES[m[2] if len(m) > 2 else "stronger"] for m in modifiers]
    if layout == "orca":  # part and modifiers as component objects 2, 3, ...; parts listed in model_settings.config
        objects, parts = [], []
        for i, (v, f) in enumerate(pieces):
            oid, label = i + 2, name if i == 0 else f"{types[i - 1][2]} {i}"
            objects.append(f"  <object id=\"{oid}\" name={quoteattr(label)} type=\"model\">{_mesh_xml(v + shift, f, paint if i == 0 else None)}</object>")
            conf = {} if i == 0 else modifiers[i - 1][1]
            sub = "normal_part" if i == 0 else types[i - 1][0]
            parts.append(f"  <part id=\"{oid}\" subtype=\"{sub}\">\n    <metadata key=\"name\" value={quoteattr(label)}/>\n{_meta(conf)}\n  </part>")
        comps = "".join(f'<component objectid="{i + 2}"/>' for i in range(len(pieces)))
        model = (MODEL_HEAD + "\n".join(objects)
                 + f"\n  <object id=\"1\" name={quoteattr(name)} type=\"model\"><components>{comps}</components></object>\n" + foot)
        config = ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<config>\n <object id=\"1\">\n"
                  f"    <metadata key=\"name\" value={quoteattr(name)}/>\n{_meta(settings)}\n" + "\n".join(parts) + "\n </object>\n</config>")
        return _zip(model, config, None, extra)

    # PrusaSlicer: one mesh, the modifiers as triangle ranges after the part's.
    verts, faces, offset, bounds = [], [], 0, [0]
    for v, f in pieces:
        verts.append(v)
        faces.append(f + offset)
        offset += len(v)
        bounds.append(bounds[-1] + len(f))
    model = (MODEL_HEAD + f"  <object id=\"1\" name={quoteattr(name)} type=\"model\">{_mesh_xml(np.concatenate(verts) + shift, np.concatenate(faces), None if paint is None else np.concatenate([paint, np.zeros(bounds[-1] - len(paint), bool)]))}</object>\n"
             + foot)
    vols = []
    for i in range(len(pieces)):
        if i == 0:
            body = f"    <metadata type=\"volume\" key=\"name\" value={quoteattr(name)}/>\n    <metadata type=\"volume\" key=\"volume_type\" value=\"ModelPart\"/>"
        else:
            sub, vtype, label = types[i - 1]
            body = (f"    <metadata type=\"volume\" key=\"name\" value=\"{label} {i}\"/>\n    <metadata type=\"volume\" key=\"volume_type\" value=\"{vtype}\"/>"
                    + (f"\n    <metadata type=\"volume\" key=\"modifier\" value=\"1\"/>\n{_meta(prusa_settings(modifiers[i - 1][1]), 'volume')}"
                       if sub == "modifier_part" else ""))
        vols.append(f"  <volume firstid=\"{bounds[i]}\" lastid=\"{bounds[i + 1] - 1}\">\n{body}\n  </volume>")
    config = (f"<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<config>\n <object id=\"1\" instances_count=\"{len(copies)}\">\n"
              f"  <metadata type=\"object\" key=\"name\" value={quoteattr(name)}/>\n{_meta(prusa_settings(settings), 'object')}\n"
              + "\n".join(vols) + "\n </object>\n</config>")
    return _zip(model, None, config, extra)


def _weld_painted(triangles: np.ndarray, paint: np.ndarray | None) -> tuple[np.ndarray, np.ndarray, np.ndarray | None]:
    """As _weld, with ``paint`` (one value per triangle) kept for the triangles that remain."""
    verts, faces, keep = _weld(triangles, keep=True)
    return verts, faces, None if paint is None else np.asarray(paint, bool)[keep]


def _weld(triangles: np.ndarray, decimals: int = 5, keep: bool = False):
    """Shared vertex list + index triangles (3MF needs an indexed mesh)."""
    pts = triangles.reshape(-1, 3)
    uniq, inverse = np.unique(np.round(pts, decimals), axis=0, return_inverse=True)
    faces = inverse.reshape(-1, 3)
    ok = (faces[:, 0] != faces[:, 1]) & (faces[:, 1] != faces[:, 2]) & (faces[:, 0] != faces[:, 2])  # collapsed: dropped
    return (uniq, faces[ok], ok) if keep else (uniq, faces[ok])
