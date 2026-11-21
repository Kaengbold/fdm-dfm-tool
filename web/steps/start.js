// Start screen (T-531, plan §3, §15.4, §16.5): what would you like to do? Two big cards (Optimize; Fix your broken
// prints = Coming soon, not clickable and not a tab stop), a slim Settings & calibration card, and "Continue" when
// this browser has unfinished work.
import { h, icon } from "../ui/dom.js";
import { registerScreen } from "../shell.js";
import { t } from "../i18n.js";

const CUBE = ["M12 3 4 7.5v9L12 21l8-4.5v-9L12 3Z", "m4 7.5 8 4.5 8-4.5M12 12v9"];
const WRENCH = "M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.6 2.6-2.4-.6-.6-2.4 2.6-2.6Z";
const GEAR = ["M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z", "M12 2v3m0 14v3M2 12h3m14 0h3M4.9 4.9 7 7m10 10 2.1 2.1M4.9 19.1 7 17m10-10 2.1-2.1"];
const ARROW = "M5 12h14m-6-6 6 6-6 6";
const CHEVRON = "m9 6 6 6-6 6";

registerScreen("start", ({ root, store, go, openRoute, services }) => {
  const s = store.get();
  const optimize = h("button", { type: "button", class: "big-card is-go", onclick: () => go(s.part ? s.step : "part") },
    h("span", { class: "big-icon" }, icon(CUBE)),
    h("span", { class: "big-title" }, t("start.optimize")),
    h("span", { class: "big-line" }, t("start.optimizeLine")),
    h("span", { class: "big-cta" }, t("start.go"), icon(ARROW)));
  const fix = h("div", { class: "big-card is-soon", "aria-disabled": "true" },
    h("span", { class: "big-icon" }, icon(WRENCH)),
    h("span", { class: "big-title" }, t("start.fix"), h("span", { class: "soon-badge" }, t("start.soon"))),
    h("ul", { class: "soon-list" }, ["start.soon1", "start.soon2", "start.soon3"].map((k) => h("li", {}, t(k)))));
  const settings = h("button", { type: "button", class: "slim-card", onclick: () => openRoute("settings") },
    icon(GEAR), h("span", { class: "slim-text" }, h("b", {}, t("shell.settings")), h("span", {}, t("start.settingsLine"))), icon(CHEVRON));
  const recent = h("section", { class: "recent", hidden: true });
  root.append(h("div", { class: "start" }, h("h1", {}, t("start.title")), h("div", { class: "big-cards" }, optimize, fix), settings, recent));

  // Unfinished work in this browser (autosave): one "Continue" card.
  services.restoreLast?.().then((last) => {
    if (!last?.state?.part) return;
    recent.hidden = false;
    recent.append(h("div", { class: "recent-head" }, h("span", { class: "ui-label" }, t("start.recent"))),
      h("button", { type: "button", class: "recent-card", onclick: () => services.continueLast(last) },
        last.thumb ? h("img", { src: last.thumb, alt: "" }) : null,
        h("b", {}, last.state.part.name),
        h("span", {}, t("start.savedAt", { when: new Date(last.savedAt).toLocaleString() }))));
  });
});
