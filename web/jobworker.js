// Jobs for a worker that answers {id, ...result} or {id, error}: used for the two OpenCascade
// builds (STEP read and write-back), which build functions from strings. The page's Content
// Security Policy forbids that; a worker started from a file of this site doesn't take it on (T-327).
// The worker starts on the first job, terminates after being idle so large WASM heaps don't sit
// in memory (T-646), and starts fresh on the next job or after a failure.
export const DEFAULT_IDLE_MS = 60_000;

export function jobWorker(url, { idleMs = DEFAULT_IDLE_MS } = {}) {
  let worker = null;
  let next = 0;
  let timeoutMs = idleMs;
  let idleTimer = null;
  const waiting = new Map();

  function clearIdle() {
    if (idleTimer != null) {
      clearTimeout(idleTimer);
      idleTimer = null;
    }
  }

  function stop(reason = "The worker stopped") {
    clearIdle();
    if (!worker) return;
    const w = worker;
    worker = null;
    for (const job of waiting.values()) job.reject(new Error(reason));
    waiting.clear();
    w.terminate();
  }

  function scheduleIdle() {
    clearIdle();
    if (!(timeoutMs > 0) || !worker || waiting.size > 0) return;
    idleTimer = setTimeout(() => {
      idleTimer = null;
      if (waiting.size === 0) stop();
    }, timeoutMs);
    idleTimer.unref?.();
  }

  function start() {
    worker = new Worker(url, { type: "module" });
    worker.onmessage = ({ data }) => {
      const job = waiting.get(data.id);
      waiting.delete(data.id);
      if (waiting.size === 0) scheduleIdle();
      if (!job) return;
      if (data.error) job.reject(new Error(data.error));
      else job.resolve(data);
    };
    worker.onerror = (e) => {  // the worker itself failed (e.g. a library file didn't load): fail every job
      e.preventDefault();
      stop(e.message || "The worker stopped");
    };
  }

  const run = (message) => {
    clearIdle();
    if (!worker) start();
    const id = ++next;
    return new Promise((resolve, reject) => {
      waiting.set(id, { resolve, reject });
      worker.postMessage({ ...message, id });
    });
  };
  run.dispose = () => stop();
  run.disposeIdle = () => { if (waiting.size === 0) stop(); };
  run.setIdleMs = (ms) => { timeoutMs = ms; if (waiting.size === 0) scheduleIdle(); };
  return run;
}

/** Fetch a pinned library file; the browser refuses it if its hash differs (same files as tools/vendor.py). */
export async function checkedFetch(url, integrity) {
  const r = await fetch(url, { integrity });
  if (!r.ok) throw new Error(`Could not load ${url} (${r.status})`);
  return r;
}

/** Answer jobs in a worker: ``work(message)`` returns {result, transfer}. */
export function answerJobs(work) {
  self.onmessage = async ({ data }) => {
    try {
      const { result, transfer = [] } = await work(data);
      self.postMessage({ ...result, id: data.id }, transfer);
    } catch (e) {
      self.postMessage({ id: data.id, error: String(e?.message ?? e) });
    }
  };
}
