"""3MF export of the oriented part, with OrcaSlicer/Bambu Studio per-object settings.

The geometry part is plain 3MF core spec and opens in any slicer. The settings are
written as per-object overrides in ``Metadata/model_settings.config``, the format
OrcaSlicer and Bambu Studio use; other slicers ignore that file.
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


def export_3mf(mesh: Mesh, name: str = "part", settings: dict[str, str] | None = None, bed_center=(128.0, 128.0)) -> bytes:
    """``mesh`` must already be oriented (lowest point at z = 0, centred on x/y = 0)."""
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
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        z.writestr("3D/3dmodel.model", model)
        z.writestr("Metadata/model_settings.config", model_settings)
    return buf.getvalue()


def _weld(triangles: np.ndarray, decimals: int = 5) -> tuple[np.ndarray, np.ndarray]:
    """Shared vertex list + index triangles (3MF needs an indexed mesh)."""
    pts = triangles.reshape(-1, 3)
    uniq, inverse = np.unique(np.round(pts, decimals), axis=0, return_inverse=True)
    faces = inverse.reshape(-1, 3)
    faces = faces[(faces[:, 0] != faces[:, 1]) & (faces[:, 1] != faces[:, 2]) & (faces[:, 0] != faces[:, 2])]
    return uniq, faces
