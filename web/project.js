// Project files shared with agents (core/fdmcore/project.py writes and reads the same format): the part as
// it is now plus the settings, loads, marks, zones, boxes and sized holes, so either side can pick up the work.

export const FORMAT = "fdm-check-project";
export const VERSION = 1;

function toBase64(bytes) {
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

/** Project JSON text. ``faceIds``: region id per triangle, so loads and marks find the same faces again. */
export function writeProject({ name, stl, faceIds, ...fields }) {
  const part = { name, stl: toBase64(stl), ...(faceIds ? { face_ids: Array.from(faceIds) } : {}) };
  const kept = Object.fromEntries(Object.entries(fields).filter(([, v]) => v != null && !(Array.isArray(v) && !v.length)
    && !(typeof v === "object" && !Array.isArray(v) && !Object.keys(v).length)));
  return JSON.stringify({ format: FORMAT, version: VERSION, part, ...kept });
}

/** The project's fields, with the part as ``name``, ``stl`` (Uint8Array) and ``faceIds`` (or null). */
export function readProject(text) {
  let data;
  try {
    data = JSON.parse(text);
  } catch {
    throw new Error("This isn't a project file (not JSON)");
  }
  if (data?.format !== FORMAT) throw new Error("This isn't an FDM Check project file");
  if ((data.version ?? 0) > VERSION) throw new Error("This project was saved by a newer version of FDM Check: reload the page to update");
  const { part, format, version, ...fields } = data;
  return { ...fields, name: part.name, stl: Uint8Array.from(atob(part.stl), (c) => c.charCodeAt(0)), faceIds: part.face_ids ?? null };
}
