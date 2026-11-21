// Toast and Modal (§1.5). Toasts sit bottom-centre of the stage (or the page), are announced politely and go
// away by themselves unless they carry an action. Modal is a real <dialog>: focus moves in, Esc closes, focus
// returns.
import { h, icon, ICONS, portal } from "./dom.js";
import { Button } from "./button.js";
import { t } from "../i18n.js";

function toastRegion(container) {
  let region = container.querySelector(":scope > .ui-toasts");
  if (!region) container.append(region = h("div", { class: "ui-toasts", role: "status", "aria-live": "polite" }));
  return region;
}

/** showToast({message, tone: info|good|warn|bad, action: {label, onClick}, timeout}) → close(). */
export function showToast({ message, tone = "info", action, timeout = action ? 0 : 5000, container = portal() }) {
  const close = () => el.remove();
  const el = h("div", { class: ["ui-toast", `is-${tone}`] },
    h("span", { class: "ui-toast__text" }, message),
    action && Button({ label: action.label, variant: "ghost", size: "small", onClick: () => { close(); action.onClick?.(); } }),
    h("button", { type: "button", class: "ui-toast__x", "aria-label": t("ui.close"), onclick: close }, icon(ICONS.close)));
  toastRegion(container).append(el);
  if (timeout) setTimeout(close, timeout);
  return close;
}

/** openModal({title, body, actions: [{label, variant, onClick → false keeps it open}]}) → the dialog. */
export function openModal({ title, body, actions = [], onClose }) {
  const back = document.activeElement;
  const dialog = h("dialog", { class: "ui-modal", "aria-label": title },
    h("header", { class: "ui-modal__head" }, h("h2", {}, title),
      h("button", { type: "button", class: "ui-modal__x", "aria-label": t("ui.close"), onclick: () => dialog.close() }, icon(ICONS.close))),
    h("div", { class: "ui-modal__body" }, body),
    actions.length ? h("footer", { class: "ui-modal__foot" }, actions.map((a) =>
      Button({ label: a.label, variant: a.variant ?? "secondary", onClick: () => { if (a.onClick?.() !== false) dialog.close(); } }))) : null);
  dialog.addEventListener("close", () => { dialog.remove(); back?.focus?.(); onClose?.(); });
  document.body.append(dialog);
  dialog.showModal();
  return dialog;
}
