// Popover3D (§8, §15.2): a small panel anchored to a 3D point (the force box at the arrow tip, the fit popover at a
// hole). The caller passes a project(point) → {x, y} function and calls update() when the camera moves; the popover
// stays inside the viewport. Enter confirms (onConfirm), Esc cancels this draft only (onCancel). Focus is trapped
// inside while open and returns to the canvas after.
import { h, portal } from "./dom.js";

const FOCUSABLE = "button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex='-1'])";

export function openPopover3D({ anchor, project, bounds, title, content, onConfirm, onCancel, offset = 16 }) {
  const back = document.activeElement;
  const pop = h("div", { class: "ui-pop", role: "dialog", "aria-label": title }, title && h("div", { class: "ui-pop__title" }, title), content);
  portal().append(pop);

  function update() {
    const { x, y } = project(anchor);
    const box = bounds?.() ?? document.documentElement.getBoundingClientRect();
    const w = pop.offsetWidth, hgt = pop.offsetHeight;
    const left = Math.min(Math.max(x + offset, box.left + 8), box.right - w - 8);
    const top = Math.min(Math.max(y - hgt / 2, box.top + 8), box.bottom - hgt - 8);
    pop.style.left = `${left}px`;
    pop.style.top = `${top}px`;
  }
  function close() {
    pop.remove();
    back?.focus?.();
  }
  pop.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(); onCancel?.(); }
    else if (e.key === "Enter" && e.target.tagName !== "BUTTON") { e.preventDefault(); if (onConfirm?.() !== false) close(); }
    else if (e.key === "Tab") {
      const els = [...pop.querySelectorAll(FOCUSABLE)];
      const i = els.indexOf(document.activeElement);
      const to = e.shiftKey ? (i <= 0 ? els.length - 1 : i - 1) : (i + 1) % els.length;
      e.preventDefault();
      els[to]?.focus();
    }
  });
  update();
  (pop.querySelector("input") ?? pop.querySelector(FOCUSABLE))?.focus();
  return { update, close, element: pop };
}
