// The new UI's one store (T-530, plan research/redesign-2026-11/PLAN-COMBINED.md §1.7): the user's inputs, results
// that remember which input revisions they were made from (so a changed input marks them stale instead of leaving
// them silently wrong), undo/redo for what the user builds (holds, loads, pending design changes), and autosave.
// No DOM: pure logic, unit-tested under Node (core/tests/test_shell_state.py).

// Which revision each input key bumps. A result records the revisions it used; any bump makes it stale.
export const INPUTS = {
  part: "model",
  design: "model",
  profile: "profile",
  goal: "goal",
  change: "goal",
  environment: "environment",
  holds: "loads",
  loads: "loads",
  noLoads: "loads",
  details: "loads",
  depth: "depth",
  option: "option",
};
// What Undo / Redo covers: things the user builds by hand, not navigation or settings (§1.7).
export const UNDOABLE = ["holds", "loads", "noLoads", "design", "pending"];
const UNDO_LIMIT = 100;

export function initialState() {
  return {
    step: "part",
    visited: ["part"],
    part: null, // {name, size: [x, y, z], triangles, faceIds?, kind: "stl" | "3mf" | "step"}
    design: [], // committed design changes (versions)
    profile: { printer: null, material: "", nozzle: "", nozzles: [] }, // "" = Decide later
    goal: "balanced", // balanced | strong | fast | light | quality
    change: "slicer", // slicer | design | both
    environment: { temp: "indoors", tempC: 20, touches: [] },
    holds: [],
    loads: [],
    noLoads: false,
    details: { duration: "often", vibration: "no" },
    depth: "extended",
    option: null,
    pending: [],
    exportOff: [], // slicer changes the user unticked on Done (left to their own profile)
  };
}

export function createStore(initial = initialState()) {
  let state = structuredClone(initial);
  const revisions = Object.fromEntries([...new Set(Object.values(INPUTS))].map((k) => [k, 0]));
  const results = new Map(); // name -> {value, deps: {revisionKey: n}}
  const listeners = new Set();
  const undoStack = [];
  const redoStack = [];

  const snapshot = () => structuredClone(Object.fromEntries(UNDOABLE.map((k) => [k, state[k]])));
  const notify = (changed) => listeners.forEach((fn) => fn(state, changed));

  function bump(keys) {
    for (const rev of new Set(keys.map((k) => INPUTS[k]).filter(Boolean))) revisions[rev] += 1;
  }

  /** Merge ``patch`` into the state. Undoable keys go on the undo stack (one step per call). */
  function set(patch, { undoable = true } = {}) {
    const keys = Object.keys(patch).filter((k) => JSON.stringify(state[k]) !== JSON.stringify(patch[k]));
    if (!keys.length) return;
    if (undoable && keys.some((k) => UNDOABLE.includes(k))) {
      undoStack.push(snapshot());
      if (undoStack.length > UNDO_LIMIT) undoStack.shift();
      redoStack.length = 0;
    }
    state = { ...state, ...structuredClone(Object.fromEntries(keys.map((k) => [k, patch[k]]))) };
    bump(keys);
    notify(keys);
  }

  function restore(from, to) {
    if (!from.length) return false;
    to.push(snapshot());
    const back = from.pop();
    const keys = Object.keys(back).filter((k) => JSON.stringify(state[k]) !== JSON.stringify(back[k]));
    state = { ...state, ...back };
    bump(keys);
    notify(keys);
    return true;
  }

  /** The revisions of the given inputs right now, e.g. deps("model", "profile"). */
  function deps(...revisionKeys) {
    return Object.fromEntries(revisionKeys.map((k) => {
      if (!(k in revisions)) throw new Error(`Unknown input: ${k}`);
      return [k, revisions[k]];
    }));
  }

  return {
    get: () => state,
    set,
    subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); },
    undo: () => restore(undoStack, redoStack),
    redo: () => restore(redoStack, undoStack),
    canUndo: () => undoStack.length > 0,
    canRedo: () => redoStack.length > 0,
    revisions: () => ({ ...revisions }),
    deps,
    /** Keep a result with the input revisions it was made from (deps(...) at the time it started). */
    setResult(name, value, usedDeps) { results.set(name, { value, deps: { ...usedDeps } }); notify([`result:${name}`]); },
    result: (name) => results.get(name)?.value,
    /** A result is stale when any input it used changed since. Missing results aren't stale (just missing). */
    isStale(name) {
      const r = results.get(name);
      return Boolean(r) && Object.entries(r.deps).some(([k, n]) => revisions[k] !== n);
    },
    /** What autosave writes: inputs only (results are recomputed). */
    toJSON: () => structuredClone(state),
    load(saved) {
      state = { ...initialState(), ...structuredClone(saved) };
      undoStack.length = redoStack.length = 0;
      results.clear();
      bump(Object.keys(INPUTS));
      notify(Object.keys(state));
    },
  };
}

/** Autosave to IndexedDB after changes settle. Fails quietly where storage is blocked (private windows, previews). */
export function autosave(store, { name = "fdm-ui-autosave", delay = 800, indexedDB: idb = globalThis.indexedDB } = {}) {
  if (!idb) return { flush: async () => false, restore: async () => null, putExtra: async () => false, getExtra: async () => null };
  const open = () => new Promise((resolve, reject) => {
    let req;
    try { req = idb.open(name, 1); } catch (e) { reject(e); return; }
    req.onupgradeneeded = () => req.result.createObjectStore("state");
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
  const write = async (key, value) => {
    try {
      const db = await open();
      await new Promise((resolve, reject) => {
        const tx = db.transaction("state", "readwrite");
        tx.objectStore("state").put(value, key);
        tx.oncomplete = resolve;
        tx.onerror = () => reject(tx.error);
      });
      db.close();
      return true;
    } catch {
      return false;
    }
  };
  const read = async (key) => {
    try {
      const db = await open();
      const value = await new Promise((resolve, reject) => {
        const req = db.transaction("state").objectStore("state").get(key);
        req.onsuccess = () => resolve(req.result ?? null);
        req.onerror = () => reject(req.error);
      });
      db.close();
      return value;
    } catch {
      return null;
    }
  };
  const put = () => write("current", { savedAt: Date.now(), state: store.toJSON() });
  let timer = null;
  store.subscribe(() => { clearTimeout(timer); timer = setTimeout(put, delay); });
  return {
    flush: () => { clearTimeout(timer); return put(); },
    restore: () => read("current"),
    /** Big things that aren't inputs (the part's file bytes, a thumbnail) under their own key. */
    putExtra: (key, value) => write(key, value),
    getExtra: (key) => read(key),
  };
}
