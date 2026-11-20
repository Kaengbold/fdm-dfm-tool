// User text by key (T-454). English only for now (PRODUCT.md); another language is one more table in
// web/messages/ with the same keys. How to use it: AGENTS.md, "User text".
//
//   t("printer.more", { count: 410 })                      -> "My printer isn't listed (+ about 410 more)…"
//   t({ key: "printer.fact.bed", params: { x, y, z } })    -> a message object, as code returns it
//   <button data-i18n="printer.use">                      -> translatePage() fills in the text
//
// Messages are strings with {name} placeholders, or {one, other, …} by the plural form of params.count.
// Parameters are formatted for the language: numbers (Intl.NumberFormat), lists of messages
// (Intl.ListFormat), and nested {key, params} messages.
import en from "./messages/en.js";

const TABLES = { en };
let lang = "en";

/** The language in use, e.g. "en". */
export const language = () => lang;

/** Switch language; unknown ones fall back to English (keys missing in a table do too). */
export function setLanguage(code) {
  lang = TABLES[code] ? code : "en";
  globalThis.document?.documentElement.setAttribute("lang", lang);
}

const isMessage = (v) => v !== null && typeof v === "object" && typeof v.key === "string";

function formatParam(value) {
  if (isMessage(value)) return t(value);
  if (Array.isArray(value)) return new Intl.ListFormat(lang, { type: "conjunction" }).format(value.map(formatParam));
  if (typeof value === "number") return new Intl.NumberFormat(lang, { maximumFractionDigits: 2 }).format(value);
  return String(value);
}

/** The text for ``key`` (or a {key, params} message) with its parameters filled in. A missing key comes back as
 * the key itself, so it shows up on the page instead of disappearing. */
export function t(key, params = {}) {
  if (isMessage(key)) return t(key.key, key.params ?? {});
  let text = TABLES[lang][key] ?? TABLES.en[key];
  if (text === undefined) return key;
  if (typeof text === "object") text = text[new Intl.PluralRules(lang).select(params.count ?? 0)] ?? text.other;
  return text.replace(/\{(\w+)\}/g, (all, name) => (name in params ? formatParam(params[name]) : all));
}

/** Fill in every element marked with data-i18n (its text), data-i18n-placeholder or data-i18n-title. */
export function translatePage(root = document) {
  for (const el of root.querySelectorAll("[data-i18n]")) el.textContent = t(el.dataset.i18n);
  for (const el of root.querySelectorAll("[data-i18n-placeholder]")) el.placeholder = t(el.dataset.i18nPlaceholder);
  for (const el of root.querySelectorAll("[data-i18n-title]")) el.title = t(el.dataset.i18nTitle);
}
