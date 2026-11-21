// Button (§1.5): primary / secondary / ghost / danger, optional icon, small size. A native <button>, so Enter,
// Space and focus work without extra code.
import { h, icon } from "./dom.js";

export function Button({ label, variant = "secondary", size, iconPath, disabled = false, onClick, title } = {}) {
  return h("button", {
    type: "button",
    class: ["ui-btn", `ui-btn--${variant}`, size === "small" && "ui-btn--small", !label && "ui-btn--icon"],
    disabled,
    title,
    "aria-label": !label ? title : null,
    onclick: onClick,
  }, iconPath && icon(iconPath), label && h("span", {}, label));
}
