// Step 3: Environment ("Where will it be used?", T-532, plan §6).
// Temperature ChoiceCards (+ custom °C NumberField), "What will it touch?" ToggleChips (with exclusive
// "None of these"), live "Suitable: …" summary from the catalog (matching optimizer.py allowed_materials),
// food-contact note, and an inline conflict card when a material locked in Setup doesn't fit.
import { registerStep } from "../shell.js";
import { t } from "../i18n.js";
import { h } from "../ui/dom.js";
import { Button } from "../ui/button.js";
import { ChoiceCards } from "../ui/choice.js";
import { ToggleChip } from "../ui/chip.js";
import { NumberField } from "../ui/numberfield.js";
import { Disclosure } from "../ui/disclosure.js";
import { evaluateMaterials, TEMP_TIERS, TOUCH_IDS } from "./args.js";

const msg = (key, params) => ({ key, params });

const TOUCH_WORD_MSG = {
  water: () => msg("env.touchWord.water"),
  outdoors: () => msg("env.touchWord.outdoors"),
  abrasion: () => msg("env.touchWord.abrasion"),
  oil_fuel: () => msg("env.touchWord.oil_fuel"),
  solvent: () => msg("env.touchWord.solvent"),
  skin: () => msg("env.touchWord.skin"),
};

function reasonMsg(reason) {
  if (reason.kind === "heat") return msg("env.reason.heat", { limit: reason.limit });
  if (reason.kind === "hardened") return msg("env.reason.hardened");
  return msg("env.reason.poor", { touch: (TOUCH_WORD_MSG[reason.touch] ?? TOUCH_WORD_MSG.water)() });
}

function render({ body, store }) {
  const s = store.get();
  const catalog = s.catalog;
  if (!catalog?.materials) {
    body.replaceChildren(h("p", { class: "hint step-loading" }, t("env.loading")));
    return;
  }

  const env = s.environment ?? { temp: "indoors", tempC: 20, touches: [] };
  const touches = (env.touches ?? []).filter((x) => TOUCH_IDS.includes(x));
  const patchEnv = (patch) => store.set({ environment: { ...env, ...patch } });

  // 1. Temperature tiers + optional Custom °C NumberField
  const tempCards = ChoiceCards({
    label: t("env.temp"),
    columns: 3,
    value: env.temp || "indoors",
    options: [
      { value: "indoors", title: t("env.temp.indoors"), meta: t("env.temp.indoors.meta") },
      { value: "warm", title: t("env.temp.warm"), meta: t("env.temp.warm.meta") },
      { value: "sun", title: t("env.temp.sun"), meta: t("env.temp.sun.meta") },
      { value: "car", title: t("env.temp.car"), meta: t("env.temp.car.meta") },
      { value: "freezing", title: t("env.temp.freezing"), meta: t("env.temp.freezing.meta") },
      { value: "custom", title: t("env.temp.custom"), meta: t("env.temp.custom.meta") },
    ],
    onChange: (temp) => {
      const preset = TEMP_TIERS[temp];
      patchEnv({ temp, tempC: preset != null ? preset : (env.tempC ?? 20) });
    },
  });
  tempCards.classList.add("step-temp-cards");
  tempCards.querySelectorAll("[role=radio]").forEach((el) => { el.dataset.focusKey = `env-temp-${el.dataset.value}`; });

  let customField = null;
  if (env.temp === "custom") {
    customField = NumberField({
      label: t("env.customTemp"),
      value: env.tempC ?? 20,
      units: [{ value: "c", label: "°C" }],
      min: -40,
      max: 250,
      onChange: (n) => {
        if (n != null && n !== env.tempC) patchEnv({ temp: "custom", tempC: n });
      },
    });
    customField.input.dataset.focusKey = "env-custom-temp";
  }

  const tempGroup = h("div", { class: "step-group" },
    h("div", { class: "ui-label" }, t("env.temp")),
    tempCards,
    customField);

  // 2. What will it touch? (7 contact chips + exclusive "None of these")
  const touchDefs = [
    { id: "water", label: t("env.touch.water") },
    { id: "food", label: t("env.touch.food") },
    { id: "outdoors", label: t("env.touch.outdoors") },
    { id: "abrasion", label: t("env.touch.abrasion") },
    { id: "oil_fuel", label: t("env.touch.oil_fuel") },
    { id: "solvent", label: t("env.touch.solvent") },
    { id: "skin", label: t("env.touch.skin") },
  ];
  const touchChips = touchDefs.map(({ id, label }) => {
    const chip = ToggleChip({
      label,
      pressed: touches.includes(id),
      onChange: (on) => {
        const next = on
          ? TOUCH_IDS.filter((k) => touches.includes(k) || k === id)
          : touches.filter((k) => k !== id);
        patchEnv({ touches: next });
      },
    });
    chip.dataset.focusKey = `env-touch-${id}`;
    chip.dataset.touch = id;
    return chip;
  });
  const noneChip = ToggleChip({
    label: t("env.touch.none"),
    pressed: touches.length === 0,
    onChange: () => {
      if (touches.length) patchEnv({ touches: [] });
      else noneChip.setAttribute("aria-pressed", "true");
    },
  });
  noneChip.dataset.focusKey = "env-touch-none";
  noneChip.dataset.touch = "none";

  const touchGroup = h("div", { class: "step-group" },
    h("div", { class: "ui-label" }, t("env.touches")),
    h("div", { class: "step-chips", role: "group", "aria-label": t("env.touches") }, ...touchChips, noneChip),
    touches.includes("food") && h("p", { class: "hint step-food-note" }, t("env.foodNote")));

  // 3. Live "Suitable: …" summary + optional "More" details + locked-material conflict card
  const evalRes = evaluateMaterials(catalog, s.profile, env);
  const topNames = evalRes.suitable.slice(0, 3).map((m) => m.name).join(", ");
  const extraCount = evalRes.suitable.length - 3;
  const liveTone = !evalRes.suitable.length ? "step-live--bad" : (!evalRes.hasVerified ? "step-live--warn" : "");

  const liveLine = h("div", { class: ["step-live", liveTone], role: "status", "aria-live": "polite", dataset: { role: "suitable" } },
    h("div", { class: "step-live__row" },
      h("span", { class: "ui-label" }, t("env.suitable")),
      evalRes.suitable.length
        ? [h("b", { class: "step-live__names" }, topNames), extraCount > 0 && h("span", { class: "step-live__more num" }, t("env.suitableMore", { count: extraCount }))]
        : h("b", { class: "step-live__names" }, t("env.suitableNone"))),
    evalRes.suitable.length > 0 && !evalRes.hasVerified && h("div", { class: "step-live__note" }, t("env.notVerified")));

  let conflictCard = null;
  if (evalRes.lockedConflict) {
    const c = evalRes.lockedConflict;
    conflictCard = h("div", { class: "step-notice step-notice--warn", role: "alert", dataset: { notice: "lock-conflict" } },
      h("div", { class: "step-notice__title" }, t("env.conflictTitle", { material: c.name })),
      h("p", {}, t("env.conflictReasons", { reasons: c.reasons.map(reasonMsg) })),
      h("div", { class: "step-notice__actions" },
        Button({
          label: t("env.letToolChoose"),
          variant: "secondary",
          size: "small",
          onClick: () => store.set({ profile: { ...s.profile, material: "" } }),
        })));
  }

  let detailsDisc = null;
  const unverifiedList = evalRes.suitable.filter((m) => !m.verified);
  if (evalRes.skipped.length || unverifiedList.length) {
    const rows = [
      ...evalRes.skipped.map((m) => h("li", {}, t("env.skippedRow", { material: m.name, reasons: m.reasons.map(reasonMsg) }))),
      ...unverifiedList.map((m) => h("li", {}, t("env.unverifiedRow", {
        material: m.name,
        touches: m.unverified.map((id) => (TOUCH_WORD_MSG[id] ?? TOUCH_WORD_MSG.water)()),
      }))),
    ];
    detailsDisc = Disclosure({
      label: t("env.moreDetails"),
      summary: evalRes.skipped.length ? t("env.moreSkipped", { count: evalRes.skipped.length }) : null,
      content: h("ul", { class: "step-mat-list" }, ...rows),
    });
  }

  const suitableBlock = h("div", { class: "step-suitable-group" }, liveLine, conflictCard, detailsDisc);
  body.replaceChildren(tempGroup, touchGroup, suitableBlock);
}

registerStep("env", { render });
