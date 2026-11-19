"""Project files shared by the web app and agents: the part as it is now (after scaling and design
changes) plus everything needed to pick up where the other one left off: printer, material and nozzle,
the build direction, loads, marked faces, stronger zones, design boxes, slicer settings and the sized
holes, screws and slots (``holes``: api.holes_state, sized again with api.restore_holes).
``edit_selections`` maps stable design edit ids to booleans; ``suggested_boxes`` maps box keys
(JSON of kind, source region, point and normal) to pending boxes including their ``use`` boolean.
Pending boxes keep their geometry because a strength result that placed one isn't saved.
``excluded_settings`` lists per-object setting keys left to the user's slicer profile.

JSON with the part as base64 binary STL, so it opens anywhere and diffs as text. Face ids in loads and
marks refer to the part's face regions, which come out the same when the same triangles load again
(``face_ids`` keeps the CAD faces of a STEP part). The web app writes the same format in JS (app.js).
"""

from __future__ import annotations

import base64
import json

FORMAT = "fdm-check-project"
VERSION = 1
FIELDS = ("profile", "mode", "up", "loads", "faces", "zones", "volumes", "settings", "holes",
          "edit_selections", "suggested_boxes", "excluded_settings", "load_settings")


def dump(name: str, stl: bytes, face_ids: list[int] | None = None, **fields) -> str:
    """Project JSON for a part (``stl``: binary STL bytes) and any of ``FIELDS``."""
    unknown = set(fields) - set(FIELDS)
    if unknown:
        raise ValueError(f"Unknown project fields: {', '.join(sorted(unknown))}")
    part = {"name": name, "stl": base64.b64encode(stl).decode("ascii")}
    if face_ids is not None:
        part["face_ids"] = [int(i) for i in face_ids]
    out = {"format": FORMAT, "version": VERSION, "part": part}
    out.update({k: v for k, v in fields.items() if v not in (None, [], {})})
    return json.dumps(out, indent=1)


def read(text: str | bytes) -> dict:
    """The project's fields, with the part as ``name``, ``stl`` (bytes) and ``face_ids`` (or None)."""
    try:
        data = json.loads(text)
    except ValueError as err:
        raise ValueError("This isn't a project file (not JSON)") from err
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ValueError("This isn't an FDM Check project file")
    if data.get("version", 0) > VERSION:
        raise ValueError("This project was saved by a newer version of FDM Check: update it to open the file")
    part = data["part"]
    out = {k: data[k] for k in FIELDS if k in data}
    return {**out, "name": part["name"], "stl": base64.b64decode(part["stl"]), "face_ids": part.get("face_ids")}
