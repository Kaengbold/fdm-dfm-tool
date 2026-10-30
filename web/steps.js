// The step flow: which steps each level has, which pane shows, where the shared blocks sit
// per level, and the step list on the left. Knows nothing about the engine: app.js tells it
// what is ready and what is being worked on.

export const STEP_NAMES = {
  part: "Your part", have: "What you have", orient: "On the bed", problems: "Problems", strength: "Will it hold?",
  design: "Design changes", settings: "Slicer settings", download: "Download", result: "Result",
};

const ALL = ["part", "have", "orient", "problems", "strength", "design", "settings", "download"];
export const LEVEL_STEPS = {
  simple: ["part", "have", "result"],
  basic: ["part", "have", "orient", "problems", "strength", "settings", "download"],
  advanced: ALL,
  expert: ALL, // one long page: every pane at once, no step list
};

// Blocks that live somewhere else than the pane they are written in, per level. Simple has no
// problems/settings/download steps: those blocks make up its Result, and the load is optional
// under "What you have".
const MOVED = {
  simple: [["blk-download", "result"], ["blk-problems", "result"], ["blk-settings", "result"], ["blk-strength", "load-optional"]],
};

// What a change in a step re-computes: shown under the step, so cause and effect are visible.
const AFFECTS = {
  have: ["orient", "problems", "strength", "settings", "result"],
  orient: ["problems", "strength", "settings"],
  design: ["problems", "strength", "settings"],
};

const CHECK = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 8.5l3 3 6-7" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>';

export class Steps {
  /** ``ready(step)``: true, or a short reason why the step can't be opened yet. */
  constructor({ ready }) {
    this.ready = ready;
    this.level = "simple";
    this.current = "part";
    this.busy = new Map(); // step -> number of running jobs that will update it
    this.panes = Object.fromEntries([...document.querySelectorAll(".pane")].map((p) => [p.dataset.pane, p]));
    this.home = Object.fromEntries([...document.querySelectorAll("[id^=blk-]")].map((b) => [b.id, b.parentElement]));
    for (const [step, list] of Object.entries(AFFECTS)) {
      const note = document.createElement("p");
      note.className = "affects";
      note.dataset.affects = list.join(" ");
      this.panes[step].append(note);
    }
    document.getElementById("step-back").onclick = () => this.go(this.neighbour(-1));
    document.getElementById("step-next").onclick = () => this.go(this.neighbour(1));
  }

  get steps() {
    return LEVEL_STEPS[this.level];
  }

  neighbour(delta) {
    return this.steps[this.steps.indexOf(this.current) + delta];
  }

  setLevel(level) {
    this.level = level;
    const moved = MOVED[level] ?? [];
    for (const [id, home] of Object.entries(this.home)) {
      const block = document.getElementById(id);
      if (block.parentElement !== home) home.append(block);
    }
    for (const [id, to] of moved) (this.panes[to] ?? document.getElementById(to)).append(document.getElementById(id));
    document.getElementById("settings-fold").open = level !== "simple";
    // Keep the "what it updates" note last in its pane after blocks moved in.
    for (const note of document.querySelectorAll(".affects")) note.parentElement.append(note);
    if (!this.steps.includes(this.current)) this.current = this.steps.includes("result") && this.ready("result") === true ? "result" : this.closest();
    this.render();
  }

  /** The furthest step that is open, for a level switch that drops the current step. */
  closest() {
    return [...this.steps].reverse().find((s) => this.ready(s) === true && s !== "result") ?? "part";
  }

  go(step) {
    if (!step || this.ready(step) !== true) return;
    this.current = step;
    this.render();
    document.querySelector(".panes").scrollTop = 0;
  }

  /** Open the step that holds ``node`` (and any closed <details> around it), e.g. after a face was tapped. */
  reveal(node) {
    if (this.level === "expert") return node.scrollIntoView({ behavior: "smooth", block: "center" });
    for (let d = node.closest("details"); d; d = d.parentElement.closest("details")) d.open = true;
    const pane = node.closest(".pane")?.dataset.pane;
    if (pane && pane !== this.current) this.go(pane);
  }

  /** ``steps`` will be updated by work that just started (``on``) or ended. */
  setBusy(steps, on) {
    for (const s of steps) {
      const n = (this.busy.get(s) ?? 0) + (on ? 1 : -1);
      if (n > 0) this.busy.set(s, n);
      else this.busy.delete(s);
    }
    this.render();
  }

  /** Names of the busy steps this level shows, for the "working" badge. */
  busyNames() {
    return this.steps.filter((s) => this.busy.has(s) && s !== this.current).map((s) => STEP_NAMES[s]);
  }

  render() {
    const expert = this.level === "expert";
    const steps = this.steps;
    const at = steps.indexOf(this.current);
    for (const [name, pane] of Object.entries(this.panes)) {
      pane.hidden = expert ? !steps.includes(name) : name !== this.current;
    }
    for (const note of document.querySelectorAll(".affects")) {
      const names = note.dataset.affects.split(" ").filter((s) => steps.includes(s)).map((s) => STEP_NAMES[s]);
      note.hidden = !names.length;
      note.textContent = `Changing this updates ${names.join(", ")}.`;
    }

    const nav = document.getElementById("steps");
    nav.replaceChildren(...steps.map((s, i) => {
      const ready = this.ready(s);
      const state = s === this.current ? "now" : this.busy.has(s) ? "busy" : ready !== true ? "locked" : i < at ? "done" : "open";
      const b = document.createElement("button");
      b.type = "button";
      b.className = `step ${state}`;
      b.disabled = ready !== true;
      if (ready !== true) b.title = ready;
      if (state === "now") b.setAttribute("aria-current", "step");
      b.innerHTML = `<span class="dot">${state === "done" ? CHECK : i + 1}</span><span class="name"></span>`;
      b.querySelector(".name").textContent = STEP_NAMES[s];
      b.onclick = () => this.go(s);
      return b;
    }));

    const back = document.getElementById("step-back");
    const next = document.getElementById("step-next");
    const following = steps[at + 1];
    back.hidden = expert || at <= 0;
    next.hidden = expert || !following;
    if (following) {
      const ready = this.ready(following);
      next.disabled = ready !== true;
      next.textContent = ready !== true ? ready : following === "result" ? "Show my result" : `Next: ${STEP_NAMES[following]}`;
    }
  }
}
