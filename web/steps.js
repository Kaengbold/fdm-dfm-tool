// The step flow: which steps each level has, which pane shows, where the shared blocks sit
// per level, and the step list on the left. Knows nothing about the engine: app.js tells it
// what is ready and what is being worked on.

export const STEP_NAMES = {
  part: "Your part", have: "What you have", orient: "On the bed", problems: "Problems", strength: "Will it hold?",
  sims: "Forces on the part", printing: "While printing", fits: "Tolerances & tuning", design: "Design changes", extra: "Advanced design changes", settings: "Slicer settings", download: "Download", result: "Result",
};

// Problems come after the simulations (T-376): the checks of loads and printing come first, then the list of
// what to fix.
const ALL = ["part", "have", "orient", "strength", "sims", "printing", "problems", "fits", "design", "extra", "settings", "download"];
export const LEVEL_STEPS = {
  simple: ["part", "have", "result"],
  basic: ["part", "have", "orient", "strength", "problems", "settings", "download"],
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
  extra: ["strength", "download"],
};

const CHECK = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 8.5l3 3 6-7" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>';

export class Steps {
  /** ``ready(step)``: true, or a short reason why the step can't be opened yet. ``changed(step)``:
   * called when another step is shown, so the 3D view can show that step's things only. */
  constructor({ ready, changed = () => {} }) {
    this.ready = ready;
    this.changed = changed;
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
    // Expert is one long page: the step list becomes its table of contents and follows the scrolling.
    this.scroller = document.querySelector(".panes");
    this.scroller.addEventListener("scroll", () => {
      if (this.level !== "expert" || this.pinned) return;
      const top = this.scroller.getBoundingClientRect().top + 80;
      const inView = this.steps.filter((s) => !this.panes[s].hidden && this.panes[s].getBoundingClientRect().top <= top).pop() ?? this.steps[0];
      if (inView !== this.current) {
        this.current = inView;
        this.renderList();
        this.shown = `${this.level}|${this.current}`;
        this.changed(this.current);
      }
    }, { passive: true });
    // Working in a section (a click, a field) makes it the current one, wherever it sits on the screen;
    // scrolling by hand lets the position decide again.
    const pin = (e) => {
      const pane = e.target.closest?.(".pane")?.dataset.pane;
      if (this.level !== "expert" || !pane) return;
      this.pinned = true;
      if (pane !== this.current) this.jumpTo(pane);
    };
    this.scroller.addEventListener("pointerdown", pin);
    this.scroller.addEventListener("focusin", pin);
    for (const ev of ["wheel", "touchmove", "keydown"]) this.scroller.addEventListener(ev, (e) => {
      if (ev !== "keydown" || ["PageDown", "PageUp", "ArrowDown", "ArrowUp", "Home", "End", " "].includes(e.key)) this.pinned = false;
    }, { passive: true });
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
    // The optional load fold only has something in it where the strength block moves in (Simple);
    // elsewhere "Will it hold?" is its own step and an empty fold would open to nothing.
    const fold = document.getElementById("load-optional");
    fold.hidden = !moved.some(([, to]) => to === "load-optional");
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
    if (this.level === "expert") {
      // Jump, and keep this step current until the user scrolls by hand (the last steps can't reach the top).
      this.pinned = true;
      this.panes[step].scrollIntoView({ behavior: "smooth", block: "start" });
      return this.jumpTo(step);
    }
    this.render();
    this.scroller.scrollTop = 0;
  }

  /** Expert: make ``step`` the current one without scrolling. */
  jumpTo(step) {
    this.current = step;
    this.renderList();
    this.shown = `${this.level}|${this.current}`;
    this.changed(this.current);
  }

  /** Open the step that holds ``node`` (and any closed <details> around it), e.g. after a face was tapped. */
  reveal(node) {
    for (let d = node.closest("details"); d; d = d.parentElement.closest("details")) d.open = true;
    const pane = node.closest(".pane")?.dataset.pane;
    if (this.level === "expert") node.scrollIntoView({ behavior: "smooth", block: "center" });
    else if (pane && pane !== this.current) this.go(pane);
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

    this.renderList();
    if (this.shown !== `${this.level}|${this.current}`) {
      this.shown = `${this.level}|${this.current}`;
      this.changed(this.current);
    }

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

  renderList() {
    const steps = this.steps;
    const at = steps.indexOf(this.current);
    const expert = this.level === "expert";
    const nav = document.getElementById("steps");
    nav.replaceChildren(...steps.map((s, i) => {
      const ready = this.ready(s);
      const state = s === this.current ? "now" : this.busy.has(s) ? "busy" : ready !== true ? "locked" : i < at && !expert ? "done" : "open";
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
  }
}
