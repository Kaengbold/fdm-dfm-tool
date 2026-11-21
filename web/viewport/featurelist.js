// Keyboard use of the 3D stage (T-540, plan §1.8): the stage is one Tab stop; inside it a searchable feature list
// lets keyboard users hover and pick faces, edges and inside corners like the mouse does. Shortcuts with the stage
// focused: X x-ray, F fit, 1 / 2 / 3 Iso / Top / Front.
import { h, uid } from "../ui/dom.js";

export function attachKeyboard(stage, picker, { label, placeholder, title }) {
  const canvas = stage.canvas;
  canvas.tabIndex = 0;
  canvas.setAttribute("role", "application");
  canvas.setAttribute("aria-label", title);
  const listId = uid("features");
  const search = h("input", { type: "text", class: "feature-search", placeholder, role: "combobox", "aria-controls": listId,
    "aria-expanded": "true", autocomplete: "off", spellcheck: false });
  const list = h("ul", { id: listId, class: "feature-list", role: "listbox", "aria-label": title });
  const panel = h("div", { class: "feature-panel", hidden: true }, search, list);
  let items = [], active = 0;

  function fill() {
    const q = search.value.trim().toLowerCase();
    items = picker.features().map((f) => ({ f, text: label(f) })).filter((x) => !q || x.text.toLowerCase().includes(q)).slice(0, 80);
    list.replaceChildren(...items.map((x, i) => h("li", { id: `${listId}-${i}`, role: "option", class: "feature-item",
      onmousedown: (e) => { e.preventDefault(); choose(i); } }, x.text)));
    move(0);
  }
  function move(i) {
    if (!items.length) { picker.showFeature(null); return; }
    active = (i + items.length) % items.length;
    list.querySelectorAll("li").forEach((li, k) => li.setAttribute("aria-selected", String(k === active)));
    search.setAttribute("aria-activedescendant", `${listId}-${active}`);
    list.children[active]?.scrollIntoView?.({ block: "nearest" });
    picker.showFeature(items[active].f);
  }
  function choose(i) {
    const f = items[i]?.f;
    if (!f) return;
    close(false);
    picker.pickFeature(f);
  }
  function open() {
    if (!picker.enabled || !picker.data) return;
    if (panel.parentElement !== stage.el) stage.el.append(panel);
    panel.hidden = false;
    search.value = "";
    fill();
    search.focus();
  }
  function close(refocus = true) {
    panel.hidden = true;
    picker.showFeature(null);
    if (refocus) canvas.focus();
  }

  canvas.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const k = e.key.toLowerCase();
    if (k === "enter" || k === " ") { e.preventDefault(); open(); }
    else if (k === "x") { e.preventDefault(); stage.setXray(!stage.xray); }
    else if (k === "f") { e.preventDefault(); stage.view("fit"); }
    else if (["1", "2", "3"].includes(k)) { e.preventDefault(); stage.view({ 1: "iso", 2: "top", 3: "front" }[k]); }
  });
  search.addEventListener("input", fill);
  search.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); move(active + 1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); move(active - 1); }
    else if (e.key === "Enter") { e.preventDefault(); choose(active); }
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(); }
    else if (e.key === "Tab") close(false);
  });
  return { open, close, panel };
}
