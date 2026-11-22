// Versions (T-564, More tools): keep this version's numbers (time, plastic, strength, open problems, setup) and
// compare versions side by side, each against the first one kept: after design changes in Problems, another option,
// or a new file of the same part. Kept in the project (state ``versions``), so they survive a reload. No slicer does this.
import { h } from "../../ui/dom.js";
import { Button } from "../../ui/button.js";
import { t } from "../../i18n.js";
import { setupLine } from "../simulate.js";

/** The numbers of the part and option as they are now. ``check``: Problems' fresh check result, or null. */
export function snapshot(s, check, label) {
  const o = s.option ?? {};
  const accepted = new Set(s.accepted ?? []);
  const problems = check ? check.check.findings.filter((f) => f.severity !== "info" && !accepted.has(f.finding_id) && !(o.fixed ?? []).includes(f.finding_id)).length : null;
  return {
    id: `v-${Date.now().toString(36)}`, label, part: s.part?.name ?? "", design: s.design?.length ?? 0,
    minutes: o.minutes ?? null, grams: o.total_g ?? o.part_g ?? null, safety: o.safety ?? null, target: o.target ?? null, problems,
    setup: s.option ? setupLine(o, s.catalog) : "",
  };
}

/** "+12 min" / "−3 g" against the first version, or "" when the same or unknown. ``digits``: rounding. */
export function delta(now, first, unit, digits = 0) {
  if (now == null || first == null) return "";
  const d = Math.round((now - first) * 10 ** digits) / 10 ** digits;
  if (!d) return "";
  return `${d > 0 ? "+" : "−"}${Math.abs(d)}${unit}`;
}

export function render({ body, store }) {
  const s = store.get();
  const list = s.versions ?? [];
  const fresh = store.isStale("check") ? null : store.result("check") ?? null;
  const name = t("tools.versions.name", { n: list.length + 1 });
  body.append(h("p", { class: "ui-hint" }, t("tools.versions.intro")),
    Button({ label: t("tools.versions.keep"), variant: "primary", onClick: () => store.set({ versions: [...list, snapshot(s, fresh, name)] }) }));
  if (!fresh) body.append(h("p", { class: "ui-hint" }, t("tools.versions.noCheck")));
  if (!list.length) return;
  const first = list[0];
  const cell = (main, d) => h("td", { class: "num" }, main, d ? h("small", { class: "tools-delta" }, ` ${d}`) : null);
  const minutes = (m) => (m == null ? "—" : m >= 60 ? t("done.hours", { h: Math.floor(m / 60), m: Math.round(m % 60) }) : t("done.minutes", { m: Math.round(m) }));
  body.append(h("table", { class: "tools-table" },
    h("thead", {}, h("tr", {}, ["version", "time", "plastic", "strength", "problems", ""].map((k) => h("th", {}, k ? t(`tools.versions.col.${k}`) : "")))),
    h("tbody", {}, list.map((v) => h("tr", {},
      h("td", {}, h("b", {}, v.label), h("small", { class: "tools-sub" }, [v.part, v.design ? t("tools.versions.changes", { count: v.design }) : null,
        v.setup].filter(Boolean).join(" · "))),
      cell(minutes(v.minutes), delta(v.minutes, first.minutes, " min")),
      cell(v.grams == null ? "—" : `${Math.round(v.grams)} g`, delta(v.grams, first.grams, " g")),
      cell(v.safety == null ? "—" : `×${v.safety}`, delta(v.safety, first.safety, "", 1)),
      cell(v.problems == null ? "—" : String(v.problems), delta(v.problems, first.problems, "")),
      h("td", {}, Button({ label: t("problems.remove"), size: "small", variant: "ghost", onClick: () => store.set({ versions: store.get().versions.filter((x) => x.id !== v.id) }) })))))));
}
