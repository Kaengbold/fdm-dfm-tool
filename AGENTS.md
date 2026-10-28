# Working on this repo (humans and AI agents)

Read this before changing anything. Several agents (Claude Code, Codex, Gemini CLI, …)
may work here in parallel; these rules keep their work from colliding.

## Coordination

1. **Pick a task from [TASKS.md](TASKS.md).** Only take tasks marked `[ ]` with no owner.
2. **Claim it first**: set `owner:` to your agent/user name and the branch, commit that
   one-line change to `main` (or ask the human to), then start work.
3. **One task = one branch** named `task/<ID>-<short-name>` (e.g. `task/T-021-text-check`).
   Keep the change scoped to the task; note unrelated problems as new tasks instead of fixing them.
4. **Don't edit files another open task obviously owns** (see the task's "files" hint). If you
   must, say so in the task line.
5. **Finish**: tests green → mark `[x]`, add one line of outcome (and PR link) → open a PR.
   If you stop early, leave `[~]` with a note on what's done and what's left.

## Project layout

| Path | What |
|---|---|
| `core/fdmcore/` | Python engine. Runs natively **and in the browser via Pyodide** |
| `core/fdmcore/data/` | `presets.yaml`, `rules.yaml` (knowledge base) |
| `core/tests/` | pytest suite |
| `web/` | Static app (no build step): `app.js` UI, `viewer.js` three.js, `worker.js` Pyodide, `geometry.js` manifold-3d, `step.js` STEP import, `assistant.js` / `gemini.js` / `openai.js` AI panel |
| `tools/` | `make_samples.py`, `make_manifest.py`, `e2e_smoke.py` |
| `research/` | Background material (unverified unless stated) |

## Hard constraints

- **Engine modules used by the web app may only import numpy, shapely, pyyaml, scipy**
  (Pyodide packages) + the standard library. scipy is loaded lazily (see `NEEDS_SCIPY` in
  `web/worker.js`). Local-only modules (`mcp_server.py`, `booleans.py`) are listed in
  `LOCAL_ONLY` in `tools/make_manifest.py`.
- **After adding/removing an engine file, run `python tools/make_manifest.py`** — a test fails otherwise.
- **A GEOS error in Pyodide kills the engine** (C++ exception, `try/except` can't catch it). Pyodide ships
  an older GEOS than pip, so code that works natively can crash in the browser. Don't snap or
  "clean" geometry speculatively; repair only invalid shapes. Run `node tools/pyodide_check.mjs`
  after touching `slicer.py` / `checks.py` geometry code.
- **Every API result must be JSON-friendly** (bytes allowed for arrays). `api.dispatch` converts numpy types.
- **New findings need a `rules.yaml` entry** with at least one `slicer` or `process` fix
  (for users who can't edit the model). Facts in rules need a source in the PR description.
- **Plain language** in everything users see: hobbyists, not engineers.
- **Licence AGPL-3.0-or-later.** Only add dependencies with compatible licences; note them in README.
- **No secrets in the repo.** API keys stay in the user's browser (localStorage, opt-in).

## Commands

```sh
cd core && python -m pytest -q                 # engine tests (~20 s)
python tools/e2e_smoke.py                      # browser test in headless Edge (pip install playwright)
python tools/e2e_smoke.py part.step --gemini-review   # + your part, + Gemini screenshot review
node tools/pyodide_check.mjs                   # engine in Pyodide under Node (once: npm install --no-save --prefix tools pyodide@0.27.2)
python tools/make_samples.py                   # regenerate web/samples (needs trimesh, manifold3d)
python -m http.server 8000                     # then open http://localhost:8000/web/
```

Run both pytest and the e2e smoke test before marking a task done if you touched `web/`
or `core/fdmcore/api.py`.

## Style

- Match the surrounding code: small functions, docstrings that say *why*, no dead code.
- Python: type hints, numpy vectorised over loops where it matters (Pyodide is ~3× slower).
- JS: plain ES modules, no framework, no build step; pin CDN versions.
- Commit messages: imperative summary line, body explains why.

## Shell gotcha (Windows / Git Bash)

Long inline `python - <<'EOF'` patch scripts sometimes break in the agent shell quoting.
Write the script to a file and run it, or use your editor tool.
