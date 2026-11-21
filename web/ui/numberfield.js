// NumberField (§1.5, §8): a number with a unit switch (e.g. N | MPa) and a quiet conversion line under it
// ("≈ 5.1 kg of weight", "3 MPa × 412 mm² = 1 236 N"). Units the context can't use are disabled with a reason.
// Enter calls onCommit, Esc onCancel (the force popover uses both). Errors show inline, never as an alert.
import { h, uid } from "./dom.js";
import { SegmentedControl } from "./choice.js";
import { t } from "../i18n.js";

/** units: [{value, label, disabled?, reason?}]; convert(value, unit) → the conversion line or "". */
export function NumberField({ label, value = "", units = [], unit = units[0]?.value, min, max, convert, onChange, onCommit, onCancel, autofocus = false }) {
  const id = uid("num"), hintId = uid("hint");
  const input = h("input", { id, type: "text", inputmode: "decimal", class: "ui-num__input num", value: String(value), autocomplete: "off", "aria-describedby": hintId });
  const hint = h("div", { id: hintId, class: "ui-num__hint", "aria-live": "polite" });
  const seg = units.length > 1 ? SegmentedControl({ label: t("ui.unitOf", { name: label }), options: units, value: unit, size: "small", onChange: (u) => { unit = u; update(); } }) : null;
  const el = h("div", { class: "ui-num" },
    h("label", { class: "ui-label", for: id }, label),
    h("div", { class: "ui-num__row" }, input, seg ?? (units[0] && h("span", { class: "ui-num__unit" }, units[0].label))),
    hint);

  function parsed() {
    const n = Number(input.value.replace(",", ".").replace(/\s/g, ""));
    return input.value.trim() === "" || !Number.isFinite(n) ? null : n;
  }
  function error() {
    const n = parsed();
    if (n == null) return input.value.trim() ? t("ui.typeNumber") : "";
    if (min != null && n < min) return t("ui.atLeast", { min });
    if (max != null && n > max) return t("ui.atMost", { max });
    return "";
  }
  function update() {
    const err = error(), n = parsed();
    el.classList.toggle("is-error", Boolean(err));
    input.setAttribute("aria-invalid", String(Boolean(err)));
    const reason = units.find((u) => u.value === unit)?.reason;
    hint.textContent = err || reason || (n != null && convert ? convert(n, unit) : "");
    if (!err) onChange?.(n, unit);
  }
  input.addEventListener("input", update);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !error() && parsed() != null) { e.preventDefault(); onCommit?.(parsed(), unit); }
    if (e.key === "Escape" && onCancel) { e.preventDefault(); e.stopPropagation(); onCancel(); }  // else the popover or dialog around it handles Esc
  });
  if (autofocus) queueMicrotask(() => { input.focus(); input.select(); });
  update();
  return Object.assign(el, { input, get value() { return parsed(); }, get unit() { return unit; } });
}
