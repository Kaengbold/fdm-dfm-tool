"""3MF export of the oriented part, with OrcaSlicer/Bambu Studio per-object settings.

The geometry part is plain 3MF core spec and opens in any slicer. The settings are
written as per-object overrides twice: ``Metadata/model_settings.config`` (OrcaSlicer and
Bambu Studio keys) and ``Metadata/Slic3r_PE_model.config`` (PrusaSlicer names, see
PRUSA_KEYS). Each slicer reads its own file. tools/slicer_check.py checks that all three apply them.

Modifier volumes (zones printed with more walls / infill) need a different file per slicer:
OrcaSlicer and Bambu Studio only read them as separate component objects, PrusaSlicer only as
triangle ranges of one mesh (each merges the other layout into the part). Without modifiers one
file serves all three.
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
    "wall_generator": "perimeter_generator",
    "bridge_speed": "bridge_speed",
}


def prusa_settings(settings: dict[str, str]) -> dict[str, str]:
    out = {PRUSA_KEYS[k]: str(v) for k, v in settings.items() if k in PRUSA_KEYS}
    if settings.get("support_type", "").startswith("tree"):
        out["support_material_style"] = "organic"
    if settings.get("ironing_type", "no ironing") != "no ironing":
        out["ironing"] = "1"
        out["ironing_type"] = settings["ironing_type"]  # same values: top | topmost | solid
    if out.get("support_material") == "1":
        out["support_material_auto"] = "1"
    return out


LAYOUTS = ("orca", "prusa")


def export_3mf(mesh: Mesh, name: str = "part", settings: dict[str, str] | None = None, bed_center=(128.0, 128.0),
               modifiers: list[tuple[np.ndarray, dict[str, str]]] = (), layout: str = "orca") -> bytes:
    """``mesh`` must already be oriented (lowest point at z = 0, centred on x/y = 0). ``modifiers``:
    (triangles in the same frame, settings) per modifier volume; ``layout`` "orca" (also Bambu
    Studio) or "prusa", see the module docstring."""
    if modifiers:
        if layout not in LAYOUTS:
            raise ValueError(f"layout must be one of {LAYOUTS}")
        return _with_modifiers(mesh, name, settings or {}, bed_center, modifiers, layout)
    verts, faces = _weld(mesh.triangles)
    verts = verts + [bed_center[0], bed_center[1], 0.0]
    vxml = "\n".join(f'<vertex x="{x:.4f}" y="{y:.4f}" z="{z:.4f}"/>' for x, y, z in verts)
    txml = "\n".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in faces)
    model = f"""<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
 <metadata name="Application">FDM DFM Tool</metadata>
 <resources>
  <object id="1" name={quoteattr(name)} type="model">
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
  <item objectid="1" printable="1"/>
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
 <object id="1" instances_count="1">
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
    return buf.getvalue()


MODEL_HEAD = """<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
 <metadata name="Application">FDM DFM Tool</metadata>
 <resources>
"""
MODEL_FOOT = """ </resources>
 <build>
  <item objectid="1" printable="1"/>
 </build>
</model>"""


def _mesh_xml(verts: np.ndarray, faces: np.ndarray) -> str:
    vxml = "\n".join(f'<vertex x="{x:.4f}" y="{y:.4f}" z="{z:.4f}"/>' for x, y, z in verts)
    txml = "\n".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in faces)
    return f"<mesh>\n<vertices>\n{vxml}\n</vertices>\n<triangles>\n{txml}\n</triangles>\n</mesh>"


def _meta(d: dict, kind: str | None = None) -> str:
    """Metadata lines; ``kind`` "object" / "volume" for PrusaSlicer's config."""
    t = f'type="{kind}" ' if kind else ""
    return "\n".join(f"    <metadata {t}key={quoteattr(k)} value={quoteattr(str(v))}/>" for k, v in d.items())


def _zip(model: str, model_settings: str | None, prusa_model: str | None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        z.writestr("3D/3dmodel.model", model)
        if model_settings:
            z.writestr("Metadata/model_settings.config", model_settings)
        if prusa_model:
            z.writestr("Metadata/Slic3r_PE_model.config", prusa_model)
    return buf.getvalue()


def _with_modifiers(mesh: Mesh, name: str, settings: dict, bed_center, modifiers, layout: str) -> bytes:
    shift = np.array([bed_center[0], bed_center[1], 0.0])
    pieces = [_weld(mesh.triangles)] + [_weld(t) for t, _ in modifiers]
    if layout == "orca":  # part and modifiers as component objects 2, 3, ...; parts listed in model_settings.config
        objects, parts = [], []
        for i, (v, f) in enumerate(pieces):
            oid, label = i + 2, name if i == 0 else f"zone {i}"
            objects.append(f"  <object id=\"{oid}\" name={quoteattr(label)} type=\"model\">{_mesh_xml(v + shift, f)}</object>")
            conf = {} if i == 0 else modifiers[i - 1][1]
            sub = "normal_part" if i == 0 else "modifier_part"
            parts.append(f"  <part id=\"{oid}\" subtype=\"{sub}\">\n    <metadata key=\"name\" value={quoteattr(label)}/>\n{_meta(conf)}\n  </part>")
        comps = "".join(f'<component objectid="{i + 2}"/>' for i in range(len(pieces)))
        model = (MODEL_HEAD + "\n".join(objects)
                 + f"\n  <object id=\"1\" name={quoteattr(name)} type=\"model\"><components>{comps}</components></object>\n" + MODEL_FOOT)
        config = ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<config>\n <object id=\"1\">\n"
                  f"    <metadata key=\"name\" value={quoteattr(name)}/>\n{_meta(settings)}\n" + "\n".join(parts) + "\n </object>\n</config>")
        return _zip(model, config, None)

    # PrusaSlicer: one mesh, the modifiers as triangle ranges after the part's.
    verts, faces, offset, bounds = [], [], 0, [0]
    for v, f in pieces:
        verts.append(v)
        faces.append(f + offset)
        offset += len(v)
        bounds.append(bounds[-1] + len(f))
    model = (MODEL_HEAD + f"  <object id=\"1\" name={quoteattr(name)} type=\"model\">{_mesh_xml(np.concatenate(verts) + shift, np.concatenate(faces))}</object>\n"
             + MODEL_FOOT)
    vols = []
    for i in range(len(pieces)):
        if i == 0:
            body = f"    <metadata type=\"volume\" key=\"name\" value={quoteattr(name)}/>\n    <metadata type=\"volume\" key=\"volume_type\" value=\"ModelPart\"/>"
        else:
            body = (f"    <metadata type=\"volume\" key=\"name\" value=\"zone {i}\"/>\n    <metadata type=\"volume\" key=\"volume_type\" value=\"ParameterModifier\"/>\n"
                    f"    <metadata type=\"volume\" key=\"modifier\" value=\"1\"/>\n{_meta(prusa_settings(modifiers[i - 1][1]), 'volume')}")
        vols.append(f"  <volume firstid=\"{bounds[i]}\" lastid=\"{bounds[i + 1] - 1}\">\n{body}\n  </volume>")
    config = ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<config>\n <object id=\"1\" instances_count=\"1\">\n"
              f"  <metadata type=\"object\" key=\"name\" value={quoteattr(name)}/>\n{_meta(prusa_settings(settings), 'object')}\n"
              + "\n".join(vols) + "\n </object>\n</config>")
    return _zip(model, None, config)


def _weld(triangles: np.ndarray, decimals: int = 5) -> tuple[np.ndarray, np.ndarray]:
    """Shared vertex list + index triangles (3MF needs an indexed mesh)."""
    pts = triangles.reshape(-1, 3)
    uniq, inverse = np.unique(np.round(pts, decimals), axis=0, return_inverse=True)
    faces = inverse.reshape(-1, 3)
    faces = faces[(faces[:, 0] != faces[:, 1]) & (faces[:, 1] != faces[:, 2]) & (faces[:, 0] != faces[:, 2])]
    return uniq, faces
