// Select (§1.5, §5): a button that opens a searchable list (combobox + listbox pattern). Optional "Decide later"
// entry, badges per option ("Owned"), and a clear button when the value is a hard lock. Keyboard: Enter, Space or
// ArrowDown opens; typing filters; arrows move; Enter picks; Esc closes and returns focus to the button.
import { h, icon, ICONS, uid, withProps } from "./dom.js";
import { t } from "../i18n.js";

export const LATER = "";  // the value of "Decide later"

/** options: [{value, label, badge?, hint?}]. */
/** ``placeholder``: what an empty required field says (default "Decide later"). */
export function Select({ label, options, value = LATER, decideLater = false, clearable = false, searchable = true, onChange, disabled = false, placeholder }) {
  const listId = uid("list");
  const all = decideLater ? [{ value: LATER, label: t("ui.decideLater"), later: true }, ...options] : options;
  const text = h("span", { class: "ui-select__text" });
  const trigger = h("button", { type: "button", class: "ui-select__button", "aria-haspopup": "listbox", "aria-expanded": "false", "aria-label": label, disabled },
    text, icon(ICONS.chevron));
  const clear = h("button", { type: "button", class: "ui-select__clear", "aria-label": t("ui.clearChoice", { name: label }), hidden: true }, icon(ICONS.close));
  const search = h("input", { type: "text", class: "ui-select__search", role: "combobox", "aria-controls": listId, "aria-expanded": "true",
    "aria-autocomplete": "list", placeholder: t("ui.search"), autocomplete: "off", spellcheck: false });
  const list = h("ul", { id: listId, role: "listbox", class: "ui-select__list", "aria-label": label });
  const empty = h("li", { class: "ui-select__empty", role: "presentation" }, t("ui.noMatches"));
  const pop = h("div", { class: "ui-select__pop", hidden: true }, searchable && search, list);
  const el = h("div", { class: "ui-select" }, trigger, clear, pop);
  let shown = [], active = 0;

  function render() {
    const current = all.find((o) => o.value === value);
    text.textContent = current?.label ?? placeholder ?? t("ui.decideLater");
    el.classList.toggle("is-later", !current || current.later === true);
    clear.hidden = !clearable || value === LATER || disabled;
  }
  function fill() {
    const q = search.value.trim().toLowerCase();
    shown = all.filter((o) => !q || o.label.toLowerCase().includes(q));
    list.replaceChildren(...shown.map((o, i) => h("li", {
      id: `${listId}-${i}`, role: "option", class: ["ui-select__option", o.later && "is-later"], "aria-selected": String(o.value === value),
      onmousedown: (e) => { e.preventDefault(); pick(o.value); },
    }, h("span", {}, o.label), o.hint && h("span", { class: "ui-select__hint" }, o.hint), o.badge && h("span", { class: "ui-badge" }, o.badge))));
    if (!shown.length) list.append(empty);
    setActive(Math.max(0, shown.findIndex((o) => o.value === value)));
  }
  function setActive(i) {
    active = Math.min(Math.max(i, 0), shown.length - 1);
    list.querySelectorAll("[role=option]").forEach((li, k) => li.classList.toggle("is-active", k === active));
    const li = list.children[active];
    search.setAttribute("aria-activedescendant", li?.id ?? "");
    li?.scrollIntoView?.({ block: "nearest" });
  }
  function open() {
    if (disabled || !pop.hidden) return;
    pop.hidden = false;
    trigger.setAttribute("aria-expanded", "true");
    search.value = "";
    fill();
    (searchable ? search : list).focus();
    document.addEventListener("pointerdown", outside, true);
  }
  function close(focus = true) {
    if (pop.hidden) return;
    pop.hidden = true;
    trigger.setAttribute("aria-expanded", "false");
    document.removeEventListener("pointerdown", outside, true);
    if (focus) trigger.focus();
  }
  function outside(e) { if (!el.contains(e.target)) close(false); }
  function pick(v) {
    const changed = v !== value;
    value = v;
    render();
    close();
    if (changed) onChange?.(v);
  }

  trigger.addEventListener("click", () => (pop.hidden ? open() : close()));
  trigger.addEventListener("keydown", (e) => { if (e.key === "ArrowDown") { e.preventDefault(); open(); } });
  clear.addEventListener("click", () => pick(LATER));
  search.addEventListener("input", fill);
  pop.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setActive(active + 1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActive(active - 1); }
    else if (e.key === "Enter") { e.preventDefault(); if (shown[active]) pick(shown[active].value); }
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(); }
    else if (e.key === "Tab") close(false);
  });
  render();
  return withProps(el, { get value() { return value; }, set value(v) { value = v; render(); }, open, close });
}
