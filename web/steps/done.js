// Done step (T-562, plan §15.1, §16.9): "Your part is ready." A summary of the chosen option, the slicer changes with
// a tick per row (unticked ones stay with the user's own slicer profile), Export 3MF for the remembered slicer, the
// changed model as STL when design changes went in, Save project, Share report and Copy CAD checklist; then the extras
// (Tuned settings & G-code with Download G-code from the browser OrcaSlicer; Tolerances and Make it lighter come with
// T-570 / T-571) and More tools. The header's Export 3MF uses the same export at any step and says what goes out.
import { h, download } from "../ui/dom.js";
import { Button } from "../ui/button.js";
import { Disclosure } from "../ui/disclosure.js";
import { SegmentedControl } from "../ui/choice.js";
import { openModal, showToast } from "../ui/overlay.js";
import { registerStep } from "../shell.js";
import { Cancelled } from "../engine.js";
import { t } from "../i18n.js";
import { thumbnail } from "../viewport/stage.js";
import { holdToEngine, loadToEngine, markedFaces, optimizeArgs, SLICERS, SLICER_KEY, targetSlicer } from "./args.js";
import { optionGcode, setupLine, simProfile } from "./simulate.js";
import { openTool } from "./tools.js";
import { exportZones } from "./tools/zones.js";

const CURA_GLOBAL_LABELS = {
  layer_height: "Layer Height",
  support_type: "Support Structure",
  brim_type: "Build Plate Adhesion Type",
  brim_width: "Brim Width",
  raft_layers: "Build Plate Adhesion Type (Raft)",
  slow_down_for_layer_cooling: "Minimum Layer Time",
};
let busy = null; // {what, text} while an export runs
let error = null;
let checked = null; // {key, check, design} when Done had to run the check itself
let gcodeCooled = false; // Download G-code on Done: plain vs cooling-optimized (T-615)

function setTargetSlicer(v) {
  try { localStorage.setItem(SLICER_KEY, v); } catch { /* storage blocked: this visit only */ }
}

const upOf = (s) => s.option?.up ?? [0, 0, 1];
const profileOf = (s) => (s.option ? simProfile(s, s.option) : optimizeArgs(s).profile);
const baseName = (s) => (s.part?.name ?? "part").replace(/\.(stl|3mf|step|stp)$/i, "");

/** The check (and design advice) for what goes out: Problems' when it's for this part and option, else run here. */
async function currentCheck(store, services) {
  if (store.result("check") && !store.isStale("check")) return store.result("check");
  const sim = store.isStale("simulate") ? null : store.result("simulate");
  const key = JSON.stringify(store.deps("model", "option", "profile"));
  if (checked?.key === key) return checked;
  const s = store.get();
  const check = sim?.check ?? await services.engine.call("check", { up: upOf(s), profile: profileOf(s) });
  checked = { key, check, design: null };
  return checked;
}

const LABELS = { wall_loops: "done.key.walls", sparse_infill_density: "done.key.infill", top_shell_layers: "done.key.top",
  bottom_shell_layers: "done.key.bottom", layer_height: "done.key.layer" };

/**
 * The slicer changes that go into the 3MF: the check's recommendations, overridden by the option's settings (which
 * carry the fixes confirmed in Problems), plus the thin-layer height ranges for shallow slopes. [{key, value, label, reason}].
 */
export function settingRows(state, check) {
  const rows = new Map();
  for (const r of check?.settings ?? []) rows.set(r.key, { key: r.key, value: String(r.value), label: r.label ?? r.key, reason: r.reason ?? "" });
  for (const [key, value] of Object.entries(state.option?.settings ?? {})) {
    if (key === "nozzle_diameter") continue; // the printer profile's, not a per-part setting
    const was = rows.get(key);
    rows.set(key, { key, value: String(value), label: was?.label ?? (LABELS[key] ? t(LABELS[key]) : key.replace(/_/g, " ")),
      reason: was && String(was.value) === String(value) ? was.reason : t("done.fromOption") });
  }
  const fine = fineLayers(check);
  if (fine.length) {
    rows.set("fine_layers", { key: "fine_layers", value: fine.map((f) => `${f.z0}–${f.z1} mm`).join(", "), label: t("done.key.fine"),
      reason: t("done.fineWhy", { height: fine[0].layer_height }) });
  }
  return [...rows.values()];
}

/** Thin layers over just the shallow slopes (shallow_slope_stairs findings' heights). */
function fineLayers(check) {
  return (check?.findings ?? []).filter((f) => f.rule === "shallow_slope_stairs" && f.metrics?.z0 != null)
    .map((f) => ({ z0: f.metrics.z0, z1: f.metrics.z1, layer_height: f.metrics.layer_height }));
}

async function run(what, text, fn, services) {
  if (busy) return;
  busy = { what, text };
  error = null;
  services.rerender();
  try {
    await fn();
  } catch (e) {
    if (!(e instanceof Cancelled)) error = t("done.failed", { reason: e.message });
  } finally {
    busy = null;
    services.rerender();
  }
}

/** Export the 3MF for ``slicer`` with the ticked slicer changes (§15.1). */
export async function export3mf(store, services, slicer = targetSlicer()) {
  const s = store.get();
  const { check } = await currentCheck(store, services);
  const off = new Set(s.exportOff ?? []);
  const rows = settingRows(s, check);
  const on = rows.filter((r) => !off.has(r.key));
  const settings = Object.fromEntries(on.filter((r) => r.key !== "fine_layers").map((r) => [r.key, r.value]));
  const bytes = await services.engine.call("export", {
    up: upOf(s), profile: profileOf(s), settings, excluded_settings: rows.filter((r) => off.has(r.key) && r.key !== "fine_layers").map((r) => r.key),
    slicer: slicer === "prusa" ? "prusa" : slicer === "cura" ? "cura" : "orca", fine_layers: off.has("fine_layers") ? [] : fineLayers(check),
    faces: markedFaces(s), // Quality's show faces: the slicer keeps the seam off them
    // stronger zones from Make it lighter, and the zones and hidden gaps from More tools (for this orientation)
    zones: [...(s.option?.zones ?? []), ...exportZones(s)],
    copies: s.copies ?? 1, pauses: s.pauses ?? [],
    cooling: s.option?.cooling ?? null, // slower hot heights from Tuned settings
  });
  // The below-target warning travels with the export (§16.2): in the file name, too.
  const warn = s.option?.warning === "below_target" ? "_below_target" : "";
  download(bytes, `${baseName(s)}${warn}.3mf`, "model/3mf");
}

/** What the header's Export 3MF sends out right now, in one line (§15.1). */
export function exportSummary(store) {
  const s = store.get();
  if (!s.option) return t("done.what.noOption");
  const parts = [t("done.what.option", { setup: setupLine(s.option, s.catalog) })];
  if (s.design?.length) parts.push(t("done.what.design", { count: s.design.length }));
  if (s.pending?.length) parts.push(t("done.what.pending", { count: s.pending.length }));
  parts.push(...extrasInUse(s));
  if (store.isStale("optimize")) parts.push(t("done.what.stale"));
  if (s.option.warning === "below_target") parts.push(t("done.what.below"));
  return parts.join(" ");
}

/** The More tools and Problems extras that go into the 3MF, one short phrase each. */
export function extrasInUse(s) {
  const out = [];
  const zones = exportZones(s).length;
  if (zones) out.push(t("done.what.zones", { count: zones }));
  if ((s.copies ?? 1) > 1) out.push(t("done.what.copies", { count: s.copies }));
  if (s.pauses?.length) out.push(t("done.what.pauses", { heights: s.pauses.map((z) => `${z} mm`).join(", ") }));
  return out;
}

/** The header's Export 3MF: what goes out, the slicer, and the button (§15.1). */
export function openExport(store, services) {
  let slicer = targetSlicer();
  const body = h("div", { class: "done-export" },
    h("p", {}, exportSummary(store)),
    SegmentedControl({ label: t("done.slicer"), size: "small", value: slicer, options: SLICERS.map((v) => ({ value: v, label: t(`done.slicer.${v}`) })),
      onChange: (v) => { slicer = v; setTargetSlicer(v); } }));
  openModal({ title: t("done.exportTitle"), body, actions: [
    { label: t("ui.cancel"), variant: "ghost" },
    { label: t("done.export"), variant: "primary", onClick: () => { run("3mf", t("done.exporting"), () => export3mf(store, services, slicer), services); } }] });
}

/** The part as it is now, as binary STL (the shown triangles in part coordinates). */
async function partStl(services) {
  const { stlBytes } = await import("../geometry.js");
  return stlBytes(services.stage.partTriangles());
}

/** The project file (project.js, the format agents read too): the part as it is now, its setup, loads and settings. */
async function projectText(store, services) {
  const s = store.get();
  const { writeProject } = await import("../project.js");
  const { check } = await currentCheck(store, services);
  const off = new Set(s.exportOff ?? []);
  const rows = settingRows(s, check).filter((r) => r.key !== "fine_layers");
  const loads = s.noLoads || s.goal === "quality" ? [] : [...s.holds.map(holdToEngine), ...s.loads.map(loadToEngine)];
  return writeProject({
    name: `${baseName(s)}.stl`, stl: await partStl(services), faceIds: s.design?.length ? null : services.getPart?.()?.faceIds,
    profile: profileOf(s), up: upOf(s), loads, faces: markedFaces(s),
    settings: Object.fromEntries(rows.filter((r) => !off.has(r.key)).map((r) => [r.key, r.value])),
    excluded_settings: rows.filter((r) => off.has(r.key)).map((r) => r.key),
  });
}

async function shareReport(store, services) {
  const s = store.get();
  const o = s.option;
  const { reportHtml } = await import("../report.js");
  const res = await currentCheck(store, services);
  const accepted = new Set(s.accepted ?? []);
  const problems = res.check.findings.filter((f) => f.severity !== "info" && !accepted.has(f.finding_id) && !(o?.fixed ?? []).includes(f.finding_id));
  const off = new Set(s.exportOff ?? []);
  const sim = store.isStale("simulate") ? null : store.result("simulate");
  const project = await projectText(store, services);
  const html = reportHtml({
    title: t("done.report.title", { name: s.part.name }),
    date: new Date().toLocaleDateString(),
    setup: o ? setupLine(o, s.catalog) : s.catalog?.printers?.[s.profile.printer]?.name ?? "",
    verdict: problems.length
      ? { kind: problems.some((f) => f.severity === "problem") ? "bad" : "warn", head: t("done.report.watch", { count: problems.length }), text: t("done.report.watchText") }
      : { kind: "ok", head: t("done.ready"), text: t("done.report.okText") },
    overview: thumbnail(services.stage.partTriangles(), 480),
    orientation: { label: o?.orientation ?? t("sim.custom"), reasons: o?.reasons ?? [] },
    stats: o ? t("done.report.stats", { time: minutes(o.minutes), grams: Math.round(o.total_g ?? o.part_g ?? 0) }) : "",
    problems: problems.map((f) => ({ title: f.title ?? f.short_title, severity: f.severity, picture: null, details: [f.one_liner ?? f.detail], why: f.why ?? "",
      fixes: (f.fixes ?? []).map((x) => ({ kind: t(`ui.tag.${x.domain === "geometry" ? "design" : x.domain}`), text: x.text })) })),
    strength: o?.safety != null ? { kind: o.meets_target === false ? "bad" : "ok", head: o.meets_target === false ? t("done.report.below") : t("done.report.strong"),
      safety: o.safety, need: o.target ?? 2, lines: [], picture: null } : null,
    warp: sim?.warp ? { kind: sim.warp.verdict === "fine" ? "ok" : "warn", head: t(`sim.verdict.${sim.warp.verdict.replace(/ /g, "_")}`), lines: [], picture: null } : null,
    design: s.design?.length || res.design?.advice?.length
      ? { items: (s.design ?? []).map((d) => ({ title: d.title, description: "" })), advice: res.design?.advice ?? [] } : null,
    settings: settingRows(s, res.check).filter((r) => !off.has(r.key)),
    project,
    appUrl: location.href.split(/[?#]/)[0],
  });
  download(html, `${baseName(s)}_report.html`, "text/html");
}

async function cadChecklist(store, services) {
  const s = store.get();
  const res = await currentCheck(store, services);
  let advice = res.design?.advice;
  if (!advice) advice = (await services.engine.call("design", { up: upOf(s), profile: profileOf(s), findings: res.check.findings })).advice ?? [];
  const lines = advice.map((a) => `- [ ] ${a.text}`);
  const text = lines.length ? [t("done.cad.head", { name: s.part.name }), ...lines].join("\n") : t("done.cad.none");
  try {
    await navigator.clipboard.writeText(text);
    showToast({ message: lines.length ? t("done.cad.copied", { count: lines.length }) : t("done.cad.none") });
  } catch {
    openModal({ title: t("done.cad.title"), body: h("pre", { class: "done-pre" }, text), actions: [{ label: t("ui.close"), variant: "primary" }] });
  }
}

async function downloadGcode(store, services, cooled = gcodeCooled) {
  const s = store.get();
  const res = await optionGcode(store, services);
  if (res?.state !== "ready") throw new Error(t(`sim.slice.fallback.${["too_big", "no_profile", "unavailable"].includes(res?.reason) ? res.reason : "failed"}`));
  if (!cooled) {
    download(res.gcode, `${baseName(s)}.gcode`, "text/x.gcode");
    return;
  }
  const o = s.option;
  const profile = { ...profileOf(s), goal: s.goal || "balanced" };
  const data = typeof res.gcode === "string" ? new TextEncoder().encode(res.gcode) : res.gcode;
  await services.engine.call("load_gcode", { up: o.up, profile }, data);
  const opt = await services.engine.call("optimize_gcode", { up: o.up, profile, loads: optimizeArgs(s).loads }, data);
  if (!opt?.ok) throw new Error((opt?.problems ?? []).join(" · ") || t("heat.fixRefused"));
  download(opt.data, `${baseName(s)}_cooled.gcode`, "text/x.gcode");
}

const minutes = (m) => (m == null ? "—" : m >= 60 ? t("done.hours", { h: Math.floor(m / 60), m: Math.round(m % 60) }) : t("done.minutes", { m: Math.round(m) }));

function summaryGrid(s) {
  const o = s.option;
  const cell = (label, value) => h("div", { class: "done-cell" }, h("span", {}, label), h("b", {}, value));
  return h("div", { class: "done-grid" },
    cell(t("done.sum.material"), o.material_name ?? s.catalog?.materials?.[o.material]?.name ?? o.material),
    cell(t("done.sum.nozzle"), t("done.sum.nozzleValue", { nozzle: o.nozzle, layer: o.layer_height })),
    cell(t("done.sum.walls"), t("done.sum.wallsValue", { walls: o.walls, infill: Math.round((o.infill ?? 0) * 100) })),
    cell(t("done.sum.time"), minutes(o.minutes)),
    cell(t("done.sum.plastic"), o.support_g ? t("done.sum.plasticSupport", { g: Math.round(o.part_g ?? o.total_g), s: Math.round(o.support_g) }) : `${Math.round(o.total_g ?? o.part_g ?? 0)} g`),
    o.safety != null ? cell(t("done.sum.safety"), t("done.sum.safetyValue", { sf: o.safety, target: o.target ?? "—" })) : cell(t("done.sum.safety"), t("done.sum.noStrength")));
}

function settingsList(store, rows) {
  const s = store.get();
  const off = new Set(s.exportOff ?? []);
  const list = h("ul", { class: "done-settings" }, rows.map((r) => h("li", {},
    h("label", {}, h("input", { type: "checkbox", checked: !off.has(r.key), onchange: (e) => {
      const next = new Set(store.get().exportOff ?? []);
      if (e.target.checked) next.delete(r.key); else next.add(r.key);
      store.set({ exportOff: [...next] });
    } }), h("span", { class: "done-settings__label" }, r.label), h("b", { class: "num" }, r.value)),
    r.reason && h("small", {}, r.reason))));
  return Disclosure({ label: t("done.changes", { count: rows.filter((r) => !off.has(r.key)).length }), summary: t("done.changesNote"), content: list });
}

registerStep("done", {
  render({ body, store, go, services }) {
    const s = store.get();
    const o = s.option;
    if (!o) {
      body.append(h("p", { class: "ui-hint" }, t("sim.noOption")), Button({ label: t("sim.toOptions"), onClick: () => go("options") }));
      return;
    }
    body.append(summaryGrid(s));
    if (o.warning === "below_target") body.append(h("p", { class: "analyse-stale" }, t("sim.belowTarget", { sf: o.safety, target: o.target })));
    if (s.pending?.length) body.append(h("p", { class: "analyse-stale" }, t("done.pendingLeft", { count: s.pending.length }), " ",
      h("button", { type: "button", class: "ui-link", onclick: () => go("problems") }, t("done.toProblems"))));

    const rowsHost = h("div", { class: "done-rows" }, h("span", { class: "ui-hint" }, t("done.loadingChanges")));
    body.append(rowsHost);
    currentCheck(store, services).then(({ check }) => rowsHost.replaceChildren(settingsList(store, settingRows(store.get(), check))))
      .catch((e) => rowsHost.replaceChildren(h("p", { class: "drop-error" }, e.message)));

    const slicer = targetSlicer();
    body.append(h("div", { class: "done-export-row" },
      SegmentedControl({ label: t("done.slicer"), size: "small", value: slicer, options: SLICERS.map((v) => ({ value: v, label: t(`done.slicer.${v}`) })),
        onChange: (v) => { setTargetSlicer(v); services.rerender(); } }),
      Button({ label: t("done.export"), variant: "primary", disabled: Boolean(busy), onClick: () => run("3mf", t("done.exporting"), () => export3mf(store, services, slicer), services) })));
    if (slicer === "cura") {
      const curaKeys = Object.entries(CURA_GLOBAL_LABELS).map(([, label]) => label).join(", ");
      body.append(h("p", { class: "ui-hint" }, t("done.curaNote", { settings: curaKeys })));
    }

    const original = s.part.original ?? s.part.name;
    const fromStep = /\.(step|stp)$/i.test(original);
    const actions = h("div", { class: "done-actions" });
    if (s.design?.length) {
      actions.append(Button({ label: t("done.stl"), disabled: Boolean(busy), onClick: () => run("stl", t("done.exporting"), async () => download(await partStl(services), `${baseName(s)}.stl`, "model/stl"), services) }));
    }
    actions.append(
      Button({ label: t("done.save"), variant: "secondary", disabled: Boolean(busy), onClick: () => run("project", t("done.saving"), async () => download(await projectText(store, services), `${baseName(s)}.fdmcheck.json`, "application/json"), services) }),
      Button({ label: t("done.share"), variant: "secondary", disabled: Boolean(busy), onClick: () => run("report", t("done.reporting"), () => shareReport(store, services), services) }),
      Button({ label: t("done.cad"), variant: "ghost", disabled: Boolean(busy), onClick: () => run("cad", t("done.cadWorking"), () => cadChecklist(store, services), services) }));
    body.append(actions);
    if (s.design?.length && fromStep) body.append(h("p", { class: "ui-hint" }, t("done.stepNote")));
    if (busy) body.append(h("div", { class: "sim-running", role: "status" }, h("span", { class: "spinner", "aria-hidden": "true" }), h("span", {}, busy.text)));
    if (error) body.append(h("p", { class: "drop-error", role: "alert" }, error));

    // Extras (§15.1): Tuned settings, Tolerances, Make it lighter; then More tools.
    const card = (title, text, action) => h("div", { class: "done-card" }, h("b", {}, title), h("span", {}, text), action);
    const extras = h("div", { class: "done-cards" },
      card(t("done.tuned.title"), o.cooling ? t("done.tuned.on") : t("done.tuned.text"), h("div", { class: "done-gcode" },
        SegmentedControl({ label: t("done.gcodeMode"), size: "small", value: gcodeCooled ? "cooled" : "plain",
          options: [{ value: "plain", label: t("done.gcodePlain") }, { value: "cooled", label: t("done.gcodeCooled") }],
          onChange: (v) => { gcodeCooled = v === "cooled"; services.rerender(); } }),
        h("div", { class: "row" },
          Button({ label: gcodeCooled ? t("done.gcodeDownloadCooled") : t("done.gcode"), size: "small", disabled: Boolean(busy),
            onClick: () => run("gcode", gcodeCooled ? t("done.slicingCooled") : t("done.slicing"), () => downloadGcode(store, services, gcodeCooled), services) }),
          Button({ label: t("done.tuned.open"), size: "small", variant: "ghost", onClick: () => go("tuned") })),
        h("small", { class: "ui-hint" }, t("done.gcodeSafety")))),
      card(t("done.tol.title"), t("done.tol.text"), Button({ label: t("done.tol.open"), size: "small", onClick: () => go("tolerances") })),
      s.goal === "quality" ? null : card(t("done.lighter.title"), o.lighter ? t("done.lighter.on") : t("done.lighter.text"),
        Button({ label: t("done.lighter.open"), size: "small", onClick: () => go("lighter") })));
    body.append(h("h3", { class: "done-h" }, t("done.extras")), extras);
    const tools = ["zones", "split", "copies", "box", "versions"].map((k) => h("li", {},
      h("div", {}, h("b", {}, t(`done.tool.${k}`)), " ", h("span", {}, t(`done.tool.${k}.text`))),
      Button({ label: t("done.tool.open"), size: "small", variant: "ghost", onClick: () => openTool(k, go) })));
    const inUse = extrasInUse(s);
    body.append(Disclosure({ label: t("done.more"), summary: inUse.length ? inUse.join(" · ") : null, open: inUse.length > 0, content: h("ul", { class: "done-tools" }, tools) }));
    if (s.pauses?.length) {
      body.append(h("p", { class: "ui-hint" }, t("done.pauses", { heights: s.pauses.map((z) => `${z} mm`).join(", ") }), " ",
        h("button", { type: "button", class: "ui-link", onclick: () => store.set({ pauses: [] }) }, t("done.pausesOff"))));
    }
  },
});
