// RadialMenu (§7): the choices for a clicked 3D spot, laid out around the cursor and kept inside the viewport.
// A key handler that opens it must call preventDefault, or the same Enter also clicks the first item.
// role=menu; arrows (any direction) cycle, Enter/Space pick, Esc or a click elsewhere closes. Focus returns to
// whatever had it before (the 3D canvas).
import { arrowIndex, h, portal } from "./dom.js";

const RADIUS = 84;

/** items: [{label, primary?, onSelect}] (the last one is usually Cancel). bounds: the viewport's DOMRect. */
export function openRadialMenu({ x, y, items, label, bounds = document.documentElement.getBoundingClientRect(), onClose }) {
  const back = document.activeElement;
  // Keep the whole ring inside the viewport (the menu flips nothing, it just moves inwards).
  const cx = Math.min(Math.max(x, bounds.left + RADIUS + 72), bounds.right - RADIUS - 72);
  const cy = Math.min(Math.max(y, bounds.top + RADIUS + 24), bounds.bottom - RADIUS - 24);
  const buttons = items.map((item, i) => {
    const angle = -Math.PI / 2 + (i * 2 * Math.PI) / items.length;  // the first item on top
    return h("button", {
      type: "button", role: "menuitem", tabIndex: -1, class: ["ui-radial__item", item.primary && "is-primary"],
      style: { left: `${Math.cos(angle) * RADIUS}px`, top: `${Math.sin(angle) * RADIUS}px` },
      onclick: () => { close(); item.onSelect?.(); },
    }, item.label);
  });
  const menu = h("div", { class: "ui-radial", role: "menu", "aria-label": label, style: { left: `${cx}px`, top: `${cy}px` } },
    h("span", { class: "ui-radial__ring", "aria-hidden": "true" }), h("span", { class: "ui-radial__dot", "aria-hidden": "true" }), buttons);
  portal().append(menu);
  buttons[0].focus();

  function close() {
    menu.remove();
    document.removeEventListener("pointerdown", outside, true);
    back?.focus?.();
    onClose?.();
  }
  function outside(e) { if (!menu.contains(e.target)) close(); }
  menu.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(); return; }
    const to = arrowIndex(e, buttons.indexOf(document.activeElement), buttons.length);
    if (to >= 0) buttons[to].focus();
    if (e.key === "Tab") e.preventDefault();  // a menu keeps focus until it closes
  });
  setTimeout(() => document.addEventListener("pointerdown", outside, true));
  return { close, element: menu };
}
