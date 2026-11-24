// Compact 2D SVG mini-animations for the Problems step (T-613).
// Only mounted inside an expanded problem row (zero work when collapsed). Hovering a fix inside that row swaps the
// diagram to show what the fix does (e.g. round hole → teardrop to stop sagging) alongside the 3D ghost on the part.
import { h } from "../ui/dom.js";
import { t } from "../i18n.js";

const SVG = "http://www.w3.org/2000/svg";

function svg(attrs, ...children) {
  const el = document.createElementNS(SVG, "svg");
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
  for (const c of children) if (c) el.append(c);
  return el;
}
function s(tag, attrs = {}) {
  const el = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
  return el;
}

/** Map a finding rule to its 2D problem animation kind (or null if none fits). */
export function problemAnimKind(finding) {
  const r = String(finding?.rule ?? "").toLowerCase();
  if (/tipping_risk|tall_thin/.test(r)) return "tipping";
  if (/small_bed_contact|print_may_fail/.test(r)) return "small_bed";
  if (/warp|sharp_corner|peel|lift|shrink/.test(r)) return "warp";
  if (/sideways_hole|hole_oval|hole_closes|hole_undersized|bridge_sag/.test(r)) return "hole_sag";
  if (/overhang|unsupported|midair|trapped_support|droop|ledge|ceiling/.test(r)) return "overhang";
  if (/thin_wall|wall_thin|gap_fuses|shallow_relief|side_detail|narrow/.test(r)) return "thin_wall";
  if (/elephant|first_layer/.test(r)) return "elephant";
  if (/weak|upright_pin|thin_neck|stress|fillet|layer_bond/.test(r)) return "weak_corner";
  if (/heat|cool|small_layers/.test(r)) return "overheat";
  return null;
}

/** Map a hovered fix to its 2D fix-preview animation kind (or null to keep the problem animation). */
export function fixAnimKind(fix) {
  if (!fix) return null;
  const text = `${fix.id ?? ""} ${fix.text ?? ""} ${Object.keys(fix.settings ?? {}).join(" ")}`.toLowerCase();
  if (/teardrop/.test(text)) return "fix_teardrop";
  if (/sacrificial|bridge_layer|cover|midair/.test(text)) return "fix_bridge";
  if (/chamfer|elephant/.test(text)) return "fix_chamfer";
  if (/brim|ear|round|fillet/.test(text)) return "fix_brim_fillet";
  if (/support/.test(text)) return "fix_support";
  if (/thicken|wall_loops|wall|layer_height|infill/.test(text)) return "fix_thicken";
  return null;
}

function drawScene(kind) {
  const W = 220, H = 76;
  const base = { viewBox: `0 0 ${W} ${H}`, class: `prob-anim__svg is-${kind}`, role: "img", "aria-hidden": "true" };
  switch (kind) {
    case "tipping":
      return svg(base,
        s("line", { x1: 16, y1: 64, x2: 204, y2: 64, class: "pa-bed" }),
        s("rect", { x: 102, y: 12, width: 16, height: 52, rx: 2, class: "pa-part pa-tip-body" }),
        s("path", { d: "M126 24 L144 24 M138 19 L144 24 L138 29", class: "pa-arrow pa-pulse" }));
    case "small_bed":
      return svg(base,
        s("line", { x1: 16, y1: 62, x2: 204, y2: 62, class: "pa-bed" }),
        s("path", { d: "M72 16 L148 16 L120 62 L100 62 Z", class: "pa-part pa-tip-body" }),
        s("line", { x1: 98, y1: 62, x2: 122, y2: 62, class: "pa-crack pa-pulse" }));
    case "warp":
      return svg(base,
        s("line", { x1: 16, y1: 60, x2: 204, y2: 60, class: "pa-bed" }),
        s("path", { d: "M34 60 L186 60 L186 24 L34 24 Z", class: "pa-part pa-warp-body" }),
        s("path", { d: "M34 56 L28 42 M186 56 L192 42", class: "pa-arrow pa-pulse" }));
    case "hole_sag":
      return svg(base,
        s("rect", { x: 40, y: 12, width: 140, height: 52, rx: 4, class: "pa-part" }),
        s("circle", { cx: 110, cy: 38, r: 18, class: "pa-void" }),
        s("path", { d: "M93 26 Q110 20 127 26", class: "pa-sag-line" }),
        s("path", { d: "M110 15 L110 27", class: "pa-arrow pa-pulse" }));
    case "overhang":
      return svg(base,
        s("line", { x1: 16, y1: 64, x2: 204, y2: 64, class: "pa-bed" }),
        s("path", { d: "M36 64 L36 22 L152 22 L152 34 L76 34 L76 64 Z", class: "pa-part" }),
        s("path", { d: "M76 34 Q114 34 152 34", class: "pa-droop-line" }),
        s("path", { d: "M122 38 L122 52", class: "pa-arrow pa-pulse" }));
    case "thin_wall":
      return svg(base,
        s("rect", { x: 86, y: 12, width: 48, height: 52, rx: 2, class: "pa-part" }),
        s("line", { x1: 94, y1: 14, x2: 94, y2: 62, class: "pa-bead" }),
        s("line", { x1: 126, y1: 14, x2: 126, y2: 62, class: "pa-bead" }),
        s("rect", { x: 102, y: 16, width: 16, height: 44, class: "pa-gap pa-pulse" }));
    case "elephant":
      return svg(base,
        s("line", { x1: 16, y1: 60, x2: 204, y2: 60, class: "pa-bed" }),
        s("rect", { x: 56, y: 18, width: 108, height: 34, class: "pa-part" }),
        s("rect", { x: 46, y: 52, width: 128, height: 8, rx: 2, class: "pa-squish" }));
    case "weak_corner":
      return svg(base,
        s("path", { d: "M42 62 L42 16 L84 16 L84 44 L178 44 L178 62 Z", class: "pa-part" }),
        s("line", { x1: 44, y1: 44, x2: 84, y2: 44, class: "pa-crack pa-pulse" }),
        s("circle", { cx: 84, cy: 44, r: 7, class: "pa-hot-dot pa-pulse" }));
    case "overheat":
      return svg(base,
        s("line", { x1: 16, y1: 64, x2: 204, y2: 64, class: "pa-bed" }),
        s("path", { d: "M96 64 L102 20 L118 20 L124 64 Z", class: "pa-part" }),
        s("ellipse", { cx: 110, cy: 22, rx: 12, ry: 7, class: "pa-hot-dot pa-pulse" }));
    case "fix_teardrop":
      return svg(base,
        s("rect", { x: 40, y: 10, width: 140, height: 56, rx: 4, class: "pa-part" }),
        s("circle", { cx: 110, cy: 42, r: 16, class: "pa-void" }),
        s("path", { d: "M96 34 L110 18 L124 34 Z", class: "pa-fix-cut" }));
    case "fix_chamfer":
      return svg(base,
        s("line", { x1: 16, y1: 62, x2: 204, y2: 62, class: "pa-bed" }),
        s("path", { d: "M44 62 L44 20 L166 20 L166 34 L82 34 L82 62 Z", class: "pa-part" }),
        s("path", { d: "M82 34 L112 34 L82 62 Z", class: "pa-fix-add" }));
    case "fix_brim_fillet":
      return svg(base,
        s("line", { x1: 16, y1: 60, x2: 204, y2: 60, class: "pa-bed" }),
        s("rect", { x: 48, y: 22, width: 124, height: 38, rx: 6, class: "pa-part" }),
        s("rect", { x: 26, y: 56, width: 168, height: 4, rx: 2, class: "pa-fix-add" }));
    case "fix_bridge":
      return svg(base,
        s("rect", { x: 44, y: 12, width: 132, height: 52, rx: 3, class: "pa-part" }),
        s("rect", { x: 74, y: 36, width: 72, height: 28, class: "pa-void" }),
        s("rect", { x: 94, y: 12, width: 32, height: 24, class: "pa-void" }),
        s("rect", { x: 74, y: 34, width: 72, height: 4, class: "pa-fix-add" }));
    case "fix_support":
      return svg(base,
        s("line", { x1: 16, y1: 64, x2: 204, y2: 64, class: "pa-bed" }),
        s("path", { d: "M36 64 L36 22 L152 22 L152 34 L76 34 L76 64 Z", class: "pa-part" }),
        s("path", { d: "M108 64 L118 34 L132 34 L142 64 Z", class: "pa-fix-add" }));
    case "fix_thicken":
      return svg(base,
        s("rect", { x: 82, y: 12, width: 56, height: 52, rx: 2, class: "pa-part" }),
        s("rect", { x: 74, y: 12, width: 8, height: 52, class: "pa-fix-add" }),
        s("rect", { x: 138, y: 12, width: 8, height: 52, class: "pa-fix-add" }));
    default:
      return null;
  }
}

/** Build the interactive 2D animation widget for an expanded problem row. Call ``setFix(fix | null)`` on hover. */
export function problemPreview(finding) {
  const baseKind = problemAnimKind(finding);
  if (!baseKind) return null;
  const host = h("figure", { class: "prob-anim", dataset: { kind: baseKind } });
  let current = null;
  function show(kind, isFix = false) {
    if (kind === current) return;
    current = kind;
    host.dataset.kind = kind;
    host.classList.toggle("is-fix", isFix);
    host.replaceChildren(drawScene(kind), h("figcaption", { class: "prob-anim__cap" }, t(`problems.anim.${kind}`)));
  }
  show(baseKind, false);
  return Object.assign(host, {
    setFix(fix) {
      const fk = fixAnimKind(fix);
      show(fk ?? baseKind, Boolean(fk));
    },
  });
}
