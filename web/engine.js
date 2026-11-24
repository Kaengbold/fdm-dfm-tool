// The new UI's connection to the Python engine (web/worker.js, Pyodide), T-531. One worker; calls are queued by the
// worker itself. Progress comes as {fraction, text}; a usable result before the call ends as onPartial (T-552). Cancelling kills the worker (Python can't be interrupted) and a
// fresh one boots; the caller gives it the part again (restore()).

export class Cancelled extends Error {}

export function createEngine({ url = new URL("./worker.js", import.meta.url), onProgress = () => {}, onPartial = () => {} } = {}) {
  let worker, ready, nextId = 0;
  const pending = new Map();
  const waiting = new Set(); // calls still waiting for the engine to be ready: a cancel then must end them too
  let restoreFn = null;

  function start() {
    worker = new Worker(url, { type: "module" });
    ready = new Promise((resolve, reject) => {
      const failAll = (msg) => {
        const err = new Error(msg);
        reject(err);
        for (const { reject: fail } of pending.values()) fail(err);
        pending.clear();
      };
      worker.addEventListener("error", (e) => failAll(e?.message || "Engine worker failed to start"));
      worker.addEventListener("messageerror", () => failAll("Engine worker message error"));
      worker.addEventListener("message", ({ data }) => {
        if (data.type === "ready") resolve();
        else if (data.type === "boot-error") reject(new Error(data.error));
        else if (data.type === "progress") onProgress({ id: data.id, fraction: data.fraction, text: data.text ?? "" });
        else if (data.type === "partial") onPartial({ id: data.id, result: data.result });
        else if (pending.has(data.id)) {
          const { resolve: ok, reject: fail } = pending.get(data.id);
          pending.delete(data.id);
          if (data.fatal) {
            fail(new Error(data.error));
            restart(); // a crashed Pyodide fails every later call
          } else if (data.error) fail(new Error(data.error));
          else ok(data.result);
        }
      });
    });
  }

  function post(method, args = {}, data = null) {
    return new Promise((resolve, reject) => {
      const id = nextId++;
      pending.set(id, { resolve, reject });
      worker.postMessage({ id, method, args, data });
    });
  }

  function restart() {
    worker.terminate();
    for (const { reject } of pending.values()) reject(new Cancelled("Cancelled"));
    pending.clear();
    // A cancel while the engine boots (e.g. right after another cancel): the old worker never gets ready.
    for (const reject of waiting) reject(new Cancelled("Cancelled"));
    waiting.clear();
    start();
    if (restoreFn) ready = ready.then(() => restoreFn(post));
  }

  start();
  return {
    /** Resolves once Python and the engine are loaded (seconds on a first visit). */
    get ready() { return ready; },
    async call(method, args = {}, data = null) {
      await new Promise((resolve, reject) => {
        const done = (fn) => (v) => { if (waiting.delete(reject)) fn(v); };
        waiting.add(reject);
        ready.then(done(resolve), done(reject));
      });
      return post(method, args, data);
    },
    /** Stop whatever runs now; a fresh engine starts and ``restore(post)`` gives it the part back. */
    cancel: restart,
    onRestore(fn) { restoreFn = fn; },
  };
}
