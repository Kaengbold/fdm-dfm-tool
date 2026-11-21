// The new UI's connection to the Python engine (web/worker.js, Pyodide), T-531. One worker; calls are queued by the
// worker itself. Progress comes as {fraction, text}. Cancelling kills the worker (Python can't be interrupted) and a
// fresh one boots; the caller gives it the part again (restore()).

export class Cancelled extends Error {}

export function createEngine({ url = new URL("./worker.js", import.meta.url), onProgress = () => {} } = {}) {
  let worker, ready, nextId = 0;
  const pending = new Map();
  let restoreFn = null;

  function start() {
    worker = new Worker(url, { type: "module" });
    ready = new Promise((resolve, reject) => {
      worker.addEventListener("message", ({ data }) => {
        if (data.type === "ready") resolve();
        else if (data.type === "boot-error") reject(new Error(data.error));
        else if (data.type === "progress") onProgress({ id: data.id, fraction: data.fraction, text: data.text ?? "" });
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
    start();
    if (restoreFn) ready = ready.then(() => restoreFn(post));
  }

  start();
  return {
    /** Resolves once Python and the engine are loaded (seconds on a first visit). */
    get ready() { return ready; },
    async call(method, args = {}, data = null) {
      await ready;
      return post(method, args, data);
    },
    /** Stop whatever runs now; a fresh engine starts and ``restore(post)`` gives it the part back. */
    cancel: restart,
    onRestore(fn) { restoreFn = fn; },
  };
}
