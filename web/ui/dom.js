// Small DOM helpers for the new UI components (T-511). No framework: each component is a function that returns
// an element (plus a few methods where it has state). Text comes in already translated (callers use t()).

let next = 0;
/** A unique id for aria wiring. */
export const uid = (prefix = "ui") => `${prefix}-${++next}`;

/** h("button", { class: "x", onclick }, "Label", child) → element. `class` may be an array; false/null children are skipped. */
export function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props ?? {})) {
    if (value == null || value === false) continue;
    if (key === "class") el.className = Array.isArray(value) ? value.filter(Boolean).join(" ") : value;
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key === "style" && typeof value === "object") Object.assign(el.style, value);
    else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
    else if (key in el && typeof value !== "string") el[key] = value;
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

/** Add ``props`` to ``el`` keeping getters and setters live (Object.assign would call a getter once and copy the
 * value, so ``field.value`` would never change). */
export function withProps(el, props) {
  return Object.defineProperties(el, Object.getOwnPropertyDescriptors(props));
}

/** Inline icon from a path list (24 × 24 view box, stroked). */
export function icon(d, label) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", "ui-icon");
  if (label) svg.setAttribute("aria-label", label);
  else svg.setAttribute("aria-hidden", "true");
  for (const path of [].concat(d)) {
    const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
    p.setAttribute("d", path);
    svg.append(p);
  }
  return svg;
}

export const ICONS = {
  chevron: "m6 9 6 6 6-6",
  check: "m5 12 5 5 9-10",
  close: "M6 6l12 12M18 6 6 18",
  pin: ["M9 4h6l-1 6 3 3H7l3-3-1-6Z", "M12 13v7"],
  search: ["M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14Z", "m20 20-3.5-3.5"],
  lock: ["M7 11V8a5 5 0 0 1 10 0v3", "M5 11h14v9H5z"],
};

/** The overlay layer for menus, popovers and toasts (§1.6). Created on first use. */
export function portal() {
  let root = document.getElementById("portal-root");
  if (!root) document.body.append(root = h("div", { id: "portal-root" }));
  return root;
}

/** Roving focus for a group of items (radio groups, menus): arrows move, Home/End jump. Returns the new index or -1. */
export function arrowIndex(e, index, count, { vertical = true, horizontal = true } = {}) {
  const keys = { Home: () => 0, End: () => count - 1 };
  if (vertical) Object.assign(keys, { ArrowDown: () => (index + 1) % count, ArrowUp: () => (index - 1 + count) % count });
  if (horizontal) Object.assign(keys, { ArrowRight: () => (index + 1) % count, ArrowLeft: () => (index - 1 + count) % count });
  const move = keys[e.key];
  if (!move) return -1;
  e.preventDefault();
  return move();
}

/** Hand the viewer a file to save (bytes or text). */
export function download(data, filename, mime = "application/octet-stream") {
  const url = URL.createObjectURL(new Blob([data], { type: mime }));
  const a = h("a", { href: url, download: filename });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
