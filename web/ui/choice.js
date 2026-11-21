// ChoiceCard group and SegmentedControl (§1.5): one choice out of a few, as a radio group with roving focus.
// Arrows move and select (like native radios), Tab leaves the group. Only the selected card shows its line (§5).
import { arrowIndex, h, uid } from "./dom.js";

function radioGroup({ label, options, value, onChange, className, render }) {
  const group = h("div", { role: "radiogroup", class: className, "aria-label": label });
  const items = options.map((opt) => {
    const el = h("button", {
      type: "button", role: "radio", class: `${className}__item`, id: uid("opt"),
      disabled: opt.disabled, dataset: { value: opt.value },
    }, render(opt));
    el.addEventListener("click", () => select(opt.value, true));
    return el;
  });
  group.append(...items);

  function select(v, fire) {
    value = v;
    items.forEach((el, i) => {
      const on = options[i].value === v;
      el.setAttribute("aria-checked", on);
      el.tabIndex = on ? 0 : -1;
    });
    if (!options.some((o) => o.value === v) && items[0]) items[0].tabIndex = 0;  // nothing picked: the first is the tab stop
    if (fire) onChange?.(v);
  }
  group.addEventListener("keydown", (e) => {
    const enabled = items.filter((el) => !el.disabled);
    const at = enabled.indexOf(document.activeElement);
    const to = arrowIndex(e, Math.max(at, 0), enabled.length);
    if (to < 0) return;
    enabled[to].focus();
    select(enabled[to].dataset.value, true);
  });
  select(value, false);
  return Object.assign(group, { get value() { return value; }, set value(v) { select(v, false); } });
}

/** Big selectable cards. options: [{value, title, line?, meta?, disabled?}]. */
export function ChoiceCards({ label, options, value, onChange, columns = 2 }) {
  const group = radioGroup({
    label, options, value, onChange, className: "ui-cards",
    render: (o) => [h("span", { class: "ui-cards__title" }, o.title), o.meta && h("span", { class: "ui-cards__meta num" }, o.meta),
      o.line && h("span", { class: "ui-cards__line" }, o.line)],
  });
  group.style.setProperty("--cols", columns);
  return group;
}

/** A compact row of choices. options: [{value, label}]. */
export function SegmentedControl({ label, options, value, onChange, size }) {
  const group = radioGroup({ label, options, value, onChange, className: "ui-seg", render: (o) => o.label });
  if (size === "small") group.classList.add("ui-seg--small");
  return group;
}
