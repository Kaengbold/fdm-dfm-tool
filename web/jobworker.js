// Jobs for a worker that answers {id, ...result} or {id, error}: used for the two OpenCascade
// builds (STEP read and write-back), which build functions from strings. The page's Content
// Security Policy forbids that; a worker started from a file of this site doesn't take it on (T-327).
// The worker starts on the first job and again after it failed.
export function jobWorker(url) {
  let worker = null;
  let next = 0;
  const waiting = new Map();

  function start() {
    worker = new Worker(url, { type: "module" });
    worker.onmessage = ({ data }) => {
      const job = waiting.get(data.id);
      waiting.delete(data.id);
      if (data.error) job.reject(new Error(data.error));
      else job.resolve(data);
    };
    worker.onerror = (e) => {  // the worker itself failed (e.g. a library file didn't load): fail every job
      e.preventDefault();
      for (const job of waiting.values()) job.reject(new Error(e.message || "The worker stopped"));
      waiting.clear();
      worker.terminate();
      worker = null;
    };
  }

  return (message) => {
    if (!worker) start();
    const id = ++next;
    return new Promise((resolve, reject) => {
      waiting.set(id, { resolve, reject });
      worker.postMessage({ ...message, id });
    });
  };
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
