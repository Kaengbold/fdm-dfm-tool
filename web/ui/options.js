// OptionColumn and ConstraintsBar (§11): the comparison columns of the Options step and the "Your choices" bar.
// Hovering or focusing a value shows a pin; pinning sends {row, value} to onPin. Results (time, safety) can't be
// pinned. "Keep all" pins the whole recipe. An editable value is a button: clicking it calls onEdit(row), and the
// step hands back the editor to show in that cell (``editing`` row key + ``editor``); an edited value is marked.
import { h, icon, ICONS } from "./dom.js";
import { Button } from "./button.js";
import { ValueChip } from "./chip.js";
import { t } from "../i18n.js";

/** option: {badge, pros[], cons[], rows: [{key, value, pinnable?, pinned?, tone?, editable?, edited?}], recommended?}. */
export function OptionColumn({ option, labels, onPin, onKeepAll, onUse, onEdit, editing = null, editor = null, needsRecheck = false }) {
  const rows = option.rows.map((row) => {
    const dataset = { row: row.key };
    if (row.key === "why") return h("div", { class: "ui-col__cell", dataset },
      h("ul", { class: "ui-col__pc" },
        (option.pros ?? []).map((p) => h("li", { class: "is-pro" }, p)),
        (option.cons ?? []).map((c) => h("li", { class: "is-con" }, c))));
    if (row.key === editing && editor) return h("div", { class: "ui-col__cell is-editing", dataset }, editor);
    const cls = ["ui-col__value", row.tone && `is-${row.tone}`, row.edited && "is-edited"];
    const value = row.editable && onEdit
      ? h("button", { type: "button", class: [...cls, "ui-col__edit"], title: t("ui.editValue"),
        "aria-label": t("ui.editValueOf", { name: labels[row.key], value: row.value }), onclick: () => onEdit(row) }, row.value)
      : h("span", { class: cls }, row.value);
    const cell = h("div", { class: ["ui-col__cell", row.pinned && "is-pinned"], dataset }, value);
    if (row.pinnable !== false) {
      cell.append(h("button", {
        type: "button", class: "ui-col__pin", "aria-pressed": String(Boolean(row.pinned)),
        "aria-label": t("ui.pinValue", { name: labels[row.key], value: row.value }),
        onclick: () => onPin?.(row),
      }, icon(ICONS.pin)));
    }
    return cell;
  });
  return h("section", { class: ["ui-col", option.recommended && "is-recommended", needsRecheck && "is-stale"], "aria-label": option.badge },
    h("header", { class: "ui-col__head" },
      h("span", { class: "ui-badge ui-col__badge" }, option.badge),
      needsRecheck ? h("span", { class: "ui-hint" }, t("ui.needsRecheck"))
        : Button({ label: t("ui.useOption"), size: "small", variant: option.recommended ? "primary" : "secondary", onClick: () => onUse?.(option) }),
      h("button", { type: "button", class: "ui-link", onclick: () => onKeepAll?.(option) }, t("ui.keepAll"))),
    rows);
}

/** choices: [{key, label}] — pinned or edited values as removable chips, plus "Re-optimize with my choices". */
export function ConstraintsBar({ choices, onRemove, onReoptimize, busy = false }) {
  return h("div", { class: "ui-constraints", role: "group", "aria-label": t("ui.yourChoices") },
    h("span", { class: "ui-label" }, t("ui.yourChoices")),
    choices.length ? choices.map((c) => ValueChip({ label: c.label, onRemove: () => onRemove?.(c) }))
      : h("span", { class: "ui-hint" }, t("ui.noChoices")),
    h("span", { class: "ui-grow" }),
    Button({ label: t("ui.reoptimize"), variant: "secondary", disabled: busy || !choices.length, onClick: onReoptimize }));
}
