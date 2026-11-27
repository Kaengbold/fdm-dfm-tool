// Popover3D (§8, §15.2): a small panel anchored to a 3D point (the force box at the arrow tip, the fit popover at a
// hole). The caller passes a project(point) → {x, y} function and calls update() when the camera moves; the popover
// stays inside the viewport. Enter confirms (onConfirm), Esc cancels this draft only (onCancel). Focus is trapped
// inside while open and returns to the canvas after.
import { h, portal } from "./dom.js";

const FOCUSABLE = "button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex='-1'])";

export function openPopover3D({ anchor, project, bounds, avoid, title, content, onConfirm, onCancel, offset = 16 }) {
  const back = document.activeElement;
  const pop = h("div", { class: "ui-pop", role: "dialog", "aria-label": title }, title && h("div", { class: "ui-pop__title" }, title), content);
  portal().append(pop);

  function update() {
    const { x, y } = project(anchor);
    const box = bounds?.() ?? document.documentElement.getBoundingClientRect();
    const w = pop.offsetWidth, hgt = pop.offsetHeight;
    const clampX = (v) => Math.min(Math.max(v, box.left + 8), box.right - w - 8);
    const clampY = (v) => Math.min(Math.max(v, box.top + 8), box.bottom - hgt - 8);
    let left = clampX(x + offset);
    let top = clampY(y - hgt / 2);
    const skip = avoid?.();
    if (skip && skip.width > 0 && skip.height > 0) {
      const pad = 8;
      const hits = (l, t) => l < skip.right + pad && l + w > skip.left - pad && t < skip.bottom + pad && t + hgt > skip.top - pad;
      if (hits(left, top)) {
        const tries = [
          [clampX(x - offset - w), top],
          [skip.right + pad, top],
          [skip.left - w - pad, top],
          [left, skip.top - hgt - pad],
          [left, skip.bottom + pad],
        ];
        const ok = tries.find(([l, t]) => l >= box.left + 8 && l + w <= box.right - 8 && t >= box.top + 8 && t + hgt <= box.bottom - 8 && !hits(l, t));
        if (ok) [left, top] = ok;
        else {
          top = skip.top - box.top >= box.bottom - skip.bottom
            ? Math.max(box.top + 8, skip.top - hgt - pad)
            : Math.min(box.bottom - hgt - 8, skip.bottom + pad);
        }
      }
    }
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
