// Load details step (T-543, plan §9): three questions, no scrolling. How long does it act (all the time = creep,
// which adjusts the strength target), the use temperature from Environment confirmed (Yes / Change), and shaking or
// knocks (light vibration with an optional mounted weight, or hits and drops with a drop height). No print-strength
// or layer-adhesion inputs.
import { h } from "../ui/dom.js";
import { Button } from "../ui/button.js";
import { ChoiceCards, SegmentedControl } from "../ui/choice.js";
import { NumberField } from "../ui/numberfield.js";
import { registerStep } from "../shell.js";
import { t } from "../i18n.js";
import { TEMP_TIERS } from "./args.js";

// The shell keeps focus on an element with data-focus-key across re-renders (typing keeps working).
const focusable = (field, key) => { field.input.dataset.focusKey = `details-${key}`; return field; };

registerStep("details", {
  render({ body, store, go }) {
    const s = store.get();
    const d = { duration: "often", vibration: "no", ...s.details };
    const set = (patch) => store.set({ details: { ...store.get().details, ...patch } });

    const duration = SegmentedControl({ label: t("details.howLong"), value: d.duration, options: [
      { value: "short", label: t("details.short") }, { value: "often", label: t("details.often") }, { value: "always", label: t("details.always") }],
      onChange: (v) => set({ duration: v }) });
    duration.dataset.focusKey = "details-duration";
    body.append(h("div", { class: "group" }, h("div", { class: "ui-label" }, t("details.howLong")), duration,
      d.duration === "always" ? h("p", { class: "ui-hint" }, t("details.alwaysHint")) : null));

    const env = s.environment ?? {};
    const tempC = env.tempC ?? TEMP_TIERS[env.temp] ?? 20;
    const tier = env.temp && env.temp !== "custom" ? t(`details.tier.${env.temp}`) : t("details.tier.custom");
    const confirm = h("div", { class: "confirm-card" },
      h("span", {}, t("details.tempCheck", { c: tempC, tier })),
      h("div", { class: "row" },
        Button({ label: t("details.yes"), variant: d.tempOk === false ? "secondary" : "secondary", size: "small", onClick: () => set({ tempOk: true }) }),
        Button({ label: t("details.change"), variant: "ghost", size: "small", onClick: () => go("env") })));
    if (d.tempOk) confirm.classList.add("is-ok");
    body.append(h("div", { class: "group" }, h("div", { class: "ui-label" }, t("details.temp")), confirm));

    const shake = SegmentedControl({ label: t("details.shake"), value: d.vibration === "no" ? "no" : "yes", options: [
      { value: "no", label: t("details.no") }, { value: "yes", label: t("details.yes") }],
      onChange: (v) => set({ vibration: v === "no" ? "no" : (store.get().details.vibration !== "no" ? store.get().details.vibration : "vibration") }) });
    const shakeGroup = h("div", { class: "group" }, h("div", { class: "ui-label" }, t("details.shake")), shake);
    if (d.vibration !== "no") {
      shakeGroup.append(ChoiceCards({ label: t("details.shakeKind"), value: d.vibration, columns: 2, options: [
        { value: "vibration", title: t("details.vibration"), line: t("details.vibrationLine") },
        { value: "drop", title: t("details.drop"), line: t("details.dropLine") }], onChange: (v) => set({ vibration: v }) }));
      shakeGroup.append(d.vibration === "vibration"
        ? focusable(NumberField({ label: t("details.mountedKg"), value: d.mountedKg ?? "", units: [{ value: "kg", label: "kg" }], min: 0, onChange: (v) => { if (v != null && v !== store.get().details.mountedKg) set({ mountedKg: v }); } }), "mounted")
        : focusable(NumberField({ label: t("details.dropMm"), value: d.dropMm ?? "", units: [{ value: "mm", label: "mm" }], min: 0, onChange: (v) => { if (v != null && v !== store.get().details.dropMm) set({ dropMm: v }); } }), "drop"));
      shakeGroup.append(h("p", { class: "ui-hint" }, t("details.shakeNote")));
    }
    body.append(shakeGroup);
  },
});
