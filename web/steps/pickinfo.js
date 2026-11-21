// Plain words for a picked spot (viewport/picker.js hit), shared by the Holds and Loads steps:
// "Top face" · "1,622 mm²", "Screw hole Ø8", "Inside corner" · "30 mm long".
import { t } from "../i18n.js";

export function describeHit(hit) {
  const i = hit.info ?? {};
  const mm = (v) => Math.round(v * 10) / 10;
  if (hit.kind === "edge") return { name: t("pick.edge"), size: t("pick.length", { length: mm(i.length_mm) }) };
  if (hit.kind === "corner") return { name: t("pick.corner"), size: t("pick.length", { length: mm(i.length_mm) }) };
  if (i.kind === "hole") return { name: t("pick.hole", { d: mm(i.hole_diameter) }), size: t("pick.area", { area: Math.round(i.area_mm2) }) };
  const nz = i.normal?.[2] ?? 0;
  const name = nz < -0.9 ? t("pick.bottomFace") : nz > 0.9 ? t("pick.topFace") : i.kind === "flat" ? t("pick.sideFace") : t("pick.roundFace");
  return { name, size: t("pick.area", { area: Math.round(i.area_mm2) }) };
}

/** The outward normal at a hit (part coordinates): the face's, or the edge's average for edges and corners. */
export function hitNormal(hit, regions) {
  const n = hit.kind === "face" ? hit.info?.normal : hit.info?.normal ?? regions?.[hit.region]?.normal;
  return n && Math.hypot(...n) > 1e-6 ? n : [0, 0, 1];
}
