// FindingRow and PendingBar (§13): a problem with its fixes directly under it, and the sticky bar for the
// pending batch of changes. Clicking a row (or Enter on it) expands it and calls onFocus, which frames the
// feature in 3D. Nothing recomputes on Add: fixes go into the pending batch.
import { h, uid } from "./dom.js";
import { Button } from "./button.js";
import { t } from "../i18n.js";

/** finding: {title, place, line, severity: bad|warn|info, state: pending|fixed|accepted|new, fixes: [{tag, text, added?, steps?}], more?}. */
export function FindingRow({ finding, expanded = false, preview = null, onFocus, onBlur, onHoverFix, onAdd, onAccept, onMore }) {
  const id = uid("find");
  const state = finding.state && finding.state !== "pending"
    ? h("span", { class: ["ui-find__state", `is-${finding.state}`] }, t(`ui.state.${finding.state}`)) : null;
  const head = h("button", { type: "button", class: "ui-find__head", "aria-expanded": String(expanded), "aria-controls": id },
    h("i", { class: ["ui-find__dot", `is-${finding.severity}`], "aria-hidden": "true" }),
    h("span", { class: "ui-find__title" }, finding.title),
    finding.place && h("span", { class: "ui-find__place num" }, finding.place),
    state);
  const fixes = finding.fixes.map((fix) => {
    const item = h("div", { class: ["ui-fix", fix.added && "is-added"] },
      h("span", { class: ["ui-tag", `is-${fix.tag}`] }, t(`ui.tag.${fix.tag}`)),
      h("span", { class: "ui-fix__text" }, fix.text),
      fix.added ? h("span", { class: "ui-fix__added" }, t("ui.added"))
        : Button({ label: fix.action ?? (fix.steps ? t("ui.showSteps") : t("ui.add")), size: "small", onClick: () => onAdd?.(fix) }));
    if (onHoverFix) {
      item.addEventListener("mouseenter", () => onHoverFix(fix));
      item.addEventListener("mouseleave", () => onHoverFix(null));
      item.addEventListener("focusin", () => onHoverFix(fix));
      item.addEventListener("focusout", (e) => { if (!item.contains(e.relatedTarget)) onHoverFix(null); });
    }
    return item;
  });
  const body = h("div", { id, class: "ui-find__body", hidden: !expanded },
    finding.line && h("p", { class: "ui-find__line" }, finding.line),
    preview,
    fixes,
    h("div", { class: "ui-find__links" },
      h("button", { type: "button", class: "ui-link", onclick: () => onMore?.(finding) }, t("ui.more")),
      h("button", { type: "button", class: "ui-link is-quiet", onclick: () => onAccept?.(finding) }, t("ui.acceptIssue"))));
  const row = h("article", { class: ["ui-find", expanded && "is-open"] }, head, body);
  head.addEventListener("click", () => {
    expanded = !expanded;
    head.setAttribute("aria-expanded", String(expanded));
    body.hidden = !expanded;
    row.classList.toggle("is-open", expanded);
    if (expanded) onFocus?.(finding);
    else onBlur?.(finding);
  });
  return row;
}

export function PendingBar({ count, onReview, onDiscard, onConfirm, conflicts = 0 }) {
  return h("div", { class: "ui-pending", role: "region", "aria-label": t("ui.pending", { count }) },
    h("b", {}, t("ui.pending", { count })),
    conflicts ? h("span", { class: "ui-pending__conflict" }, t("ui.conflicts", { count: conflicts })) : null,
    Button({ label: t("ui.review"), variant: "ghost", size: "small", onClick: onReview }),
    Button({ label: t("ui.discard"), variant: "ghost", size: "small", onClick: onDiscard }),
    Button({ label: t("ui.confirmAll"), variant: "primary", disabled: conflicts > 0, onClick: onConfirm }));
}
