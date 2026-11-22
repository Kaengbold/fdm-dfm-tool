// Problems & fixes step (T-561, plan §13). The list groups the chosen option's problems by impact (Can fail · Shape off
// · Looks rough · Weak · Info), one short row per place; opening a row frames that feature and shows its fixes first
// (Slicer / Design / Process), then More. Nothing recomputes while picking: every added fix goes into one pending
// batch (design changes as ghosts on the part, settings as old → new). Confirm all changes applies the design changes
// with the usual safety checks (web/geometry.js), reloads the changed model, carries holds and loads over and runs a
// quick re-check: the print checks, strength at the chosen settings (a drop below the target needs an OK) and an
// orientation hint that never turns the part. "Apply without re-checking" leaves the results stale instead. Undo batch
// puts the last version back. The logic without the page is in fixes.js.
import { h } from "../ui/dom.js";
import { Button } from "../ui/button.js";
import { FindingRow, PendingBar } from "../ui/finding.js";
import { Disclosure } from "../ui/disclosure.js";
import { openModal } from "../ui/overlay.js";
import { registerStep } from "../shell.js";
import { Cancelled } from "../engine.js";
import { t } from "../i18n.js";
import { holdToEngine, loadToEngine, markedFaces } from "./args.js";
import { setupLine, simProfile } from "./simulate.js";
import { trianglesOf } from "./part.js";
import * as F from "./fixes.js";

const DEPENDS = ["model", "option"];
const f32 = (b) => new Float32Array(new Uint8Array(b).slice().buffer);

let work = null; // {text} while checking or applying
let error = null;
let openId = null; // the expanded finding
let skipped = null; // the input revisions when the user applied without re-checking (then the stale list stays)
let batch = null; // the last confirmed batch: {undo, summary}
let ghostKey = null;
let designOk = false; // "This changes your model" was confirmed once
let svc = null; // the shell's services and go, kept from the last render for Next's confirm and the batch card
let goTo = null;

const profileOf = (s) => simProfile(s, s.option);
const depsKey = (store) => JSON.stringify(store.deps(...DEPENDS));

/** Holds and loads for the strength check, or null when strength isn't part of this goal. */
function strengthLoads(s) {
  if (s.goal === "quality" || s.noLoads || !s.loads.length) return null;
  return [...s.holds.map(holdToEngine), ...s.loads.map(loadToEngine)];
}

/** api.design's result with the edit meshes as Float32Array (the worker hands bytes back). */
function slimDesign(design) {
  return { edits: (design.edits ?? []).map((e) => ({ ...e, mesh: f32(e.mesh) })), advice: design.advice ?? [] };
}

async function checkAndDesign(engine, s, given = null) {
  const profile = profileOf(s);
  const check = given ?? await engine.call("check", { up: s.option.up, profile, faces: markedFaces(s) });
  const design = await engine.call("design", { up: s.option.up, profile, findings: check.findings });
  return { check, design: slimDesign(design) };
}

/** The problems of the chosen option: Simulation's check when it's fresh (it may have used the real G-code), else ours. */
async function runCheck(store, services) {
  const s = store.get();
  const deps = store.deps(...DEPENDS);
  work = { text: t("problems.checking") };
  error = null;
  services.rerender();
  try {
    const sim = store.isStale("simulate") ? null : store.result("simulate");
    const out = await checkAndDesign(services.engine, s, sim?.check ?? null);
    store.setResult("check", { ...out, detailed: Boolean(sim?.detailed) }, deps);
    skipped = null;
  } catch (e) {
    if (!(e instanceof Cancelled)) error = e.message;
  } finally {
    work = null;
    services.rerender();
  }
}

/** A modal with buttons that resolves to the chosen value (null when closed). */
function ask(title, body, actions) {
  return new Promise((resolve) => {
    let chosen = null;
    openModal({ title, body, actions: actions.map((a) => ({ ...a, onClick: () => { chosen = a.value; } })), onClose: () => resolve(chosen) });
  });
}

const upAngle = (a, b) => {
  const la = Math.hypot(...a) || 1, lb = Math.hypot(...b) || 1;
  return (Math.acos(Math.min(1, Math.max(-1, (a[0] * b[0] + a[1] * b[1] + a[2] * b[2]) / (la * lb)))) * 180) / Math.PI;
};

// ---------- confirming the batch ----------

async function confirmBatch(store, services, recheck) {
  const { engine, stage } = services;
  const s = store.get();
  const o = s.option;
  const res = store.result("check");
  const undo = { soup: stage.partTriangles(), source: services.getPart?.(), part: s.part, holds: s.holds, loads: s.loads, option: o,
    design: s.design, pending: s.pending, check: res, skipped };
  const summary = { recheck, fixed: 0, newIds: [], refused: [], lost: 0, safety: null, before: o.safety ?? null, target: o.target ?? null, better: null };
  error = null;
  try {
    const designItems = s.pending.filter((p) => p.kind === "design");
    const edits = designItems.flatMap((p) => p.editIds.map((id) => res?.design?.edits.find((e) => e.id === id)).filter(Boolean));
    const patch = { pending: [] };
    if (edits.length) {
      work = { text: t("problems.applying") };
      services.rerender();
      const { applyEdits, stlBytes } = await import("../geometry.js");
      const { soup, refused } = await applyEdits(undo.soup, edits);
      summary.refused = refused.map((r) => ({ title: r.title, reason: r.reason }));
      const applied = designItems.filter((p) => !p.editIds.every((id) => refused.some((r) => r.id === id)));
      if (applied.length) {
        const carry = s.holds.length || s.loads.length;
        const regionsBefore = carry ? await engine.call("regions") : null;
        const orientBefore = recheck ? await engine.call("orientations", { profile: profileOf(s), top: 3 }) : null;
        const original = s.part.original ?? s.part.name;
        const name = `${original.replace(/\.(stl|3mf|step|stp)$/i, "")}_dfm.stl`;
        const bytes = stlBytes(soup);
        const info = await engine.call("load", { name }, bytes);
        services.setPart?.({ name, bytes, kind: "stl" });
        stage.showPart(trianglesOf(info));
        patch.part = { ...s.part, name, kind: "stl", original, size: info.size_mm, faces: info.faces, volume: info.volume_mm3 };
        patch.design = [...s.design, ...applied.map((p) => ({ id: p.id, title: p.title, findingId: p.findingId }))];
        if (carry) {
          const regionsAfter = await engine.call("regions");
          const holds = F.carryOver(s.holds, regionsBefore, regionsAfter);
          const loads = F.carryOver(s.loads, regionsBefore, regionsAfter);
          Object.assign(patch, { holds: holds.items, loads: loads.items });
          summary.lost = holds.lost + loads.lost;
        }
        if (orientBefore) {
          const after = await engine.call("orientations", { profile: profileOf(s), top: 3 });
          const b0 = orientBefore.orientations?.[0], b1 = after.orientations?.[0];
          if (b0 && b1 && upAngle(b0.up, b1.up) > 10 && upAngle(b1.up, o.up) > 10) summary.better = b1.label;
        }
      }
    }
    let option = F.applySettings(o, s.pending);
    const next = { ...s, ...patch, option };
    if (recheck) {
      work = { text: t("problems.rechecking") };
      services.rerender();
      const out = await checkAndDesign(engine, next);
      const loads = strengthLoads(next);
      if (loads) {
        const st = await engine.call("strength", { up: o.up, loads, profile: profileOf(next), walls: option.walls, infill: option.infill, top: option.top, bottom: option.bottom });
        summary.safety = Math.round(st.safety_factor * 10) / 10;
        option = { ...option, safety: summary.safety, meets_target: summary.target == null || summary.safety >= summary.target };
      }
      const before = res?.check?.findings ?? [];
      summary.newIds = F.newProblems(before, out.check.findings);
      summary.fixed = F.fixedCount(before, out.check.findings, s.pending);
      const drop = summary.safety != null && summary.target != null && summary.safety < summary.target && (summary.before ?? Infinity) >= summary.target;
      if (drop) {
        work = null;
        services.rerender();
        const keep = await ask(t("problems.dropTitle"), t("problems.dropBody", { sf: summary.safety, target: summary.target }),
          [{ label: t("problems.undoBatch"), value: false }, { label: t("problems.keepAnyway"), variant: "primary", value: true }]);
        if (!keep) {
          batch = { undo, summary };
          await undoBatch(store, services);
          return;
        }
        option = { ...option, warning: "below_target" };
      }
      store.set({ ...patch, option }, { undoable: false });
      store.setResult("check", { ...out, detailed: false }, store.deps(...DEPENDS));
      skipped = null;
    } else {
      store.set({ ...patch, option }, { undoable: false });
      skipped = depsKey(store);
    }
    batch = { undo, summary };
  } catch (e) {
    // Failure or cancel: the last valid version stays (§13).
    batch = { undo, summary };
    await undoBatch(store, services);
    if (!(e instanceof Cancelled)) error = t("problems.applyFailed", { reason: e.message });
  } finally {
    work = null;
    services.rerender();
  }
}

async function undoBatch(store, services) {
  const u = batch?.undo;
  if (!u) return;
  const { engine, stage } = services;
  // The engine may hold the changed model before the store does (a declined strength drop, a failure mid-way).
  if ((store.get().part !== u.part || services.getPart?.() !== u.source) && u.soup) {
    const { stlBytes } = await import("../geometry.js");
    const info = await engine.call("load", { name: u.part.name }, stlBytes(u.soup));
    if (u.source) services.setPart?.(u.source);
    stage.showPart(trianglesOf(info));
  }
  store.set({ part: u.part, holds: u.holds, loads: u.loads, option: u.option, design: u.design, pending: u.pending }, { undoable: false });
  if (u.check) store.setResult("check", u.check, store.deps(...DEPENDS));
  skipped = u.skipped;
  batch = null;
  services.rerender();
}

// ---------- the list ----------

function addFix(fix, finding, store) {
  const s = store.get();
  if (fix.steps) {
    openModal({ title: fix.cad ? t("problems.cadTitle") : t("problems.stepsTitle"), body: h("p", {}, fix.text), actions: [{ label: t("ui.close"), variant: "primary" }] });
    return;
  }
  const add = () => store.set({ pending: [...s.pending, F.pendingItem(fix, finding, s.option)] });
  if (fix.changesModel && !designOk) {
    openModal({ title: t("problems.modelTitle"), body: h("p", {}, t("problems.modelBody")), actions: [
      { label: t("ui.cancel"), variant: "ghost" },
      { label: t("ui.add"), variant: "primary", onClick: () => { designOk = true; add(); } }] });
    return;
  }
  add();
}

function showMore(f) {
  const body = h("div", { class: "prob-more" },
    f.why && h("p", {}, f.why),
    f.detail && h("p", { class: "ui-hint" }, f.detail),
    (f.fixes ?? []).filter((x) => x.domain === "geometry").map((x) => h("p", {}, h("b", {}, t("ui.tag.design")), " ", x.text)));
  openModal({ title: f.title ?? f.short_title, body, actions: [{ label: t("ui.close"), variant: "primary" }] });
}

function review(store) {
  const s = store.get();
  const clash = new Set(F.conflicts(s.pending).flatMap((c) => c.ids));
  const list = h("ul", { class: "prob-review" }, s.pending.map((p) => h("li", { class: clash.has(p.id) ? "is-conflict" : null },
    h("span", { class: ["ui-tag", p.kind === "design" ? "is-design" : "is-slicer"] }, t(p.kind === "design" ? "ui.tag.design" : "ui.tag.slicer")),
    h("span", { class: "prob-review__text" }, p.kind === "design" ? p.title
      : p.changes.map((c) => t("problems.change", { key: c.key, from: c.from ?? "—", to: c.to })).join(", ")),
    Button({ label: t("problems.remove"), size: "small", variant: "ghost", onClick: () => {
      store.set({ pending: store.get().pending.filter((x) => x.id !== p.id) });
      document.querySelector("dialog.ui-modal")?.close();
      review(store);
    } }))));
  openModal({ title: t("ui.pending", { count: s.pending.length }), body: list, actions: [{ label: t("ui.close"), variant: "primary" }] });
}

function frameFinding(f, res, services) {
  const stage = services.stage;
  const rotation = res?.check?.rotation;
  if (!f.bbox || !rotation || !stage.part) return;
  const [lo, hi] = F.turnedBounds(stage.partTriangles(), rotation);
  stage.frameBox(F.boxToPart(f.bbox, rotation, lo, hi));
}

function showGhosts(store, services) {
  const s = store.get();
  const res = store.result("check");
  const key = JSON.stringify([s.pending.map((p) => p.id), store.deps(...DEPENDS)]);
  if (key === ghostKey) return;
  ghostKey = key;
  const edits = s.pending.filter((p) => p.kind === "design").flatMap((p) => p.editIds.map((id) => res?.design?.edits.find((e) => e.id === id)).filter(Boolean));
  services.stage.showGhosts(edits.flatMap((e) => {
    let start = 0;
    return e.pieces.map((n, i) => {
      const piece = { mesh: e.mesh.subarray(start * 9, (start + n) * 9), op: e.piece_ops?.[i] ?? e.op };
      start += n;
      return piece;
    });
  }));
}

function batchCard(store, services) {
  const sum = batch.summary;
  const card = h("div", { class: "prob-batch", role: "status" });
  if (sum.recheck) {
    card.append(h("b", {}, t("problems.rechecked", { fixed: sum.fixed, count: sum.newIds.length })));
    if (sum.safety != null) {
      card.append(h("span", {}, sum.before != null
        ? t("problems.safetyWas", { sf: sum.safety, was: sum.before, target: sum.target })
        : t("problems.safety", { sf: sum.safety, target: sum.target })));
    }
  } else {
    card.append(h("b", {}, t("problems.appliedNoCheck")));
  }
  if (sum.better) card.append(h("span", {}, t("problems.better", { label: sum.better }), " ",
    h("button", { type: "button", class: "ui-link", onclick: () => goTo?.("analyse") }, t("problems.viewOptions"))));
  if (sum.lost) card.append(h("span", { class: "is-warn" }, t("problems.lost", { count: sum.lost })));
  for (const r of sum.refused) card.append(h("span", { class: "is-warn" }, t("problems.refused", { title: r.title, reason: r.reason })));
  card.append(h("div", {}, Button({ label: t("problems.undoBatch"), size: "small", variant: "ghost", onClick: () => undoBatch(store, services) })));
  return card;
}

registerStep("problems", {
  render({ body, stage, store, go, services }) {
    svc = services;
    goTo = go;
    const s = store.get();
    const o = s.option;
    if (!o) {
      body.append(h("p", { class: "ui-hint" }, t("sim.noOption")), Button({ label: t("sim.toOptions"), onClick: () => go("options") }));
      return;
    }
    const res = store.result("check");
    const stale = store.isStale("check");
    if (!work && (!res || (stale && skipped !== depsKey(store)))) Promise.resolve().then(() => !work && runCheck(store, services));

    body.append(h("div", { class: "sim-setup" }, h("span", {}, setupLine(o, s.catalog)),
      h("button", { type: "button", class: "ui-link", onclick: () => go("options") }, t("sim.change"))));
    if (o.warning === "below_target") body.append(h("p", { class: "analyse-stale" }, t("sim.belowTarget", { sf: o.safety, target: o.target })));
    if (work) {
      body.append(h("div", { class: "sim-running", role: "status" }, h("span", { class: "spinner", "aria-hidden": "true" }), h("span", {}, work.text),
        Button({ label: t("ui.cancel"), variant: "ghost", onClick: () => services.engine.cancel() })));
    }
    if (error) body.append(h("p", { class: "drop-error", role: "alert" }, error));
    if (res && stale && !work) {
      body.append(h("div", { class: "analyse-stale prob-stale" }, h("span", {}, t("problems.stale")),
        Button({ label: t("problems.recheck"), size: "small", onClick: () => runCheck(store, services) })));
    }
    if (batch && !work) body.append(batchCard(store, services));
    if (!res) return;

    const ctx = { option: o, accepted: s.accepted ?? [], newIds: batch?.summary.newIds ?? [], pending: s.pending };
    const findings = res.check.findings;
    const left = F.openProblems(findings, ctx);
    if (ctx.newIds.length) body.append(h("p", { class: "prob-new", role: "alert" }, t("problems.caused", { count: ctx.newIds.length })));
    const modelLocked = s.change === "slicer";
    const row = (f) => FindingRow({
      finding: {
        title: f.short_title ?? f.title, place: f.instance_label, line: f.one_liner ?? f.detail, severity: F.severityOf(f),
        state: ((st) => (st === "open" ? "pending" : st))(F.statusOf(f, ctx)),
        fixes: F.fixesFor(f, res.design, { modelLocked }).map((fx) => ({ ...fx, added: s.pending.some((p) => p.id === fx.id),
          text: fx.changesModel ? t("problems.changesModel", { text: fx.text }) : fx.text })),
      },
      expanded: openId === f.finding_id,
      onFocus: () => { openId = f.finding_id; frameFinding(f, res, services); },
      onAdd: (fx) => addFix(F.fixesFor(f, res.design, { modelLocked }).find((x) => x.id === fx.id), f, store),
      onAccept: () => store.set({ accepted: [...new Set([...(s.accepted ?? []), f.finding_id])] }),
      onMore: () => showMore(f),
    });
    const groups = F.groupFindings(findings);
    const worse = groups.some((g) => g.impact !== "info");
    const list = h("div", { class: "prob-list" });
    for (const g of groups) {
      const rows = h("div", { class: "prob-rows" }, g.findings.map(row));
      const label = t(`problems.group.${g.impact}`);
      if (g.impact === "info" && worse) list.append(Disclosure({ label, summary: String(g.findings.length), content: rows }));
      else list.append(h("section", { class: "prob-group" }, h("h3", {}, label, " ", h("small", {}, String(g.findings.length))), rows));
    }
    if (!left.length) {
      const notRun = [];
      if (s.goal === "quality" || s.noLoads) notRun.push(t("problems.notRun.strength"));
      if (!res.detailed) notRun.push(t("problems.notRun.gcode"));
      body.append(h("div", { class: "prob-clear" }, h("b", {}, t("problems.none")), notRun.length ? h("span", { class: "ui-hint" }, notRun.join(" ")) : null));
    }
    body.append(list);

    if (s.pending.length) {
      const clashes = F.conflicts(s.pending).length;
      body.append(h("div", { class: "prob-bar" },
        PendingBar({ count: s.pending.length, conflicts: clashes, onReview: () => review(store), onDiscard: () => store.set({ pending: [] }),
          onConfirm: () => !work && confirmBatch(store, services, true) }),
        h("button", { type: "button", class: "ui-link is-quiet", disabled: clashes > 0 || Boolean(work), onclick: () => confirmBatch(store, services, false) }, t("problems.noRecheck"))));
    }
    showGhosts(store, services);
  },
  next({ store }) {
    const s = store.get();
    if (!s.pending.length) return { label: t("problems.toDone"), to: "done" };
    return {
      label: t("problems.toDone"), to: "done",
      confirm: async (go) => {
        const choice = await ask(t("problems.leaveTitle"), t("problems.leaveBody", { count: s.pending.length }), [
          { label: t("ui.discard"), variant: "ghost", value: "discard" }, { label: t("ui.confirmAll"), variant: "primary", value: "confirm" }]);
        if (choice === "discard") { store.set({ pending: [] }); go(); }
        if (choice === "confirm") {
          if (svc) await confirmBatch(store, svc, true);
          if (!store.get().pending.length) go();
        }
      },
    };
  },
  leave({ services }) {
    services.stage.clearGhosts();
    ghostKey = null;
  },
});
