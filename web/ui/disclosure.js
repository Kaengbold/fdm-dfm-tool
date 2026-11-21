// Disclosure (§1.4 "details behind More"): a button with aria-expanded and the region it shows. Not a raw <details>,
// so it looks the same everywhere and can carry a summary on the right.
import { h, icon, ICONS, uid } from "./dom.js";

export function Disclosure({ label, summary, open = false, content, onToggle }) {
  const id = uid("disc");
  const body = h("div", { id, class: "ui-disc__body", role: "region", hidden: !open }, content);
  const head = h("button", { type: "button", class: "ui-disc__head", "aria-expanded": String(open), "aria-controls": id },
    h("span", { class: "ui-disc__label" }, label), summary && h("span", { class: "ui-disc__summary" }, summary), icon(ICONS.chevron));
  head.addEventListener("click", () => {
    open = !open;
    head.setAttribute("aria-expanded", String(open));
    body.hidden = !open;
    onToggle?.(open);
  });
  return h("div", { class: "ui-disc" }, head, body);
}
