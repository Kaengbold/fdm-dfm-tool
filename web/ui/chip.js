// Chip (§1.5): a toggle (multi-select, e.g. "What will it touch?") or a removable value ("Material: PLA ×").
import { h, icon, ICONS } from "./dom.js";
import { t } from "../i18n.js";

/** Toggle chip: aria-pressed, Space/Enter toggle (native button). */
export function ToggleChip({ label, pressed = false, onChange }) {
  const el = h("button", { type: "button", class: "ui-chip ui-chip--toggle", "aria-pressed": String(pressed) }, label);
  el.addEventListener("click", () => {
    const on = el.getAttribute("aria-pressed") !== "true";
    el.setAttribute("aria-pressed", String(on));
    onChange?.(on);
  });
  return el;
}

/** Value chip with a remove button (Delete or Backspace on the chip removes it too). */
export function ValueChip({ label, onRemove }) {
  const remove = h("button", { type: "button", class: "ui-chip__x", "aria-label": t("ui.removeItem", { name: label }), onclick: () => onRemove?.() }, icon(ICONS.close));
  const el = h("span", { class: "ui-chip ui-chip--value" }, h("span", {}, label), remove);
  remove.addEventListener("keydown", (e) => { if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); onRemove?.(); } });
  return el;
}
