"""Local mode for people who aren't developers (T-450): ``fdmdfm open part.stl`` serves the web app from this
computer with the part in it, ``fdmdfm setup`` adds the MCP server to the AI agents it finds (after asking).

Works from a checkout (web/ and agents/ next to core/) and from the installed ``fdmdfm`` package, which carries
them as ``fdmdfm_web`` and ``fdmdfm_agents`` (pyproject.toml at the repository root).
"""

from __future__ import annotations

import json
import mimetypes
import shutil
import subprocess
import sys
import threading
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

ENGINE = Path(__file__).resolve().parent
# A fixed port keeps the page's address the same every time, so what the browser saved for it (your
# printers and materials, the last projects) is still there next time.
PORT = 8771
SERVER_NAME = "fdm-dfm"


def _bundled(name: str, checkout: str) -> Path:
    """A folder shipped with the package (``fdmdfm_web``), or the same folder of a checkout."""
    try:
        import importlib.util

        spec = importlib.util.find_spec(name)
        if spec and spec.submodule_search_locations:
            return Path(next(iter(spec.submodule_search_locations)))
    except (ImportError, ValueError):
        pass
    path = ENGINE.parents[1] / checkout
    if not path.is_dir():
        raise FileNotFoundError(f"Can't find the {checkout} folder: reinstall fdmdfm (see the README, local mode)")
    return path


def web_dir() -> Path:
    return _bundled("fdmdfm_web", "web")


def agents_dir() -> Path:
    return _bundled("fdmdfm_agents", "agents")


class _Handler(SimpleHTTPRequestHandler):
    """The app under /web/, the engine under /core/fdmcore/ (where the app looks for it), the part under /part/."""

    def __init__(self, *args, web: Path, part: Path | None, **kw):
        self.web, self.part = web, part
        super().__init__(*args, directory=str(web), **kw)

    def translate_path(self, path: str) -> str:
        p = unquote(urlsplit(path).path)
        for prefix, root in (("/web/", self.web), ("/core/fdmcore/", ENGINE)):
            if p.startswith(prefix):
                target = (root / p[len(prefix):]).resolve()
                return str(target) if target.is_relative_to(root.resolve()) else ""
        if self.part is not None and p == f"/part/{self.part.name}":
            return str(self.part)
        return ""  # nothing else on this computer is served

    def do_GET(self):
        if urlsplit(self.path).path in ("", "/"):
            self.send_response(302)
            self.send_header("Location", "/web/")
            self.end_headers()
            return
        if not self.translate_path(self.path):
            self.send_error(404)
            return
        super().do_GET()

    def log_message(self, *args):  # quiet: the terminal shows the address and how to stop
        pass


class _Server(ThreadingHTTPServer):
    # Not shared: on Windows address reuse lets a second server bind the same port and answer half the requests.
    allow_reuse_address = False
    daemon_threads = True


mimetypes.add_type("text/javascript", ".js")  # Windows registries sometimes say text/plain: modules then won't load
mimetypes.add_type("application/wasm", ".wasm")


def serve(part: Path | None = None, port: int = PORT, open_browser: bool = True) -> tuple[ThreadingHTTPServer, str]:
    """Start the local page (on 127.0.0.1 only) and return the server and the address that opens ``part``."""
    if part is not None:
        part = Path(part).expanduser().resolve()
        if not part.is_file():
            raise FileNotFoundError(f"No such file: {part}")
    handler = partial(_Handler, web=web_dir(), part=part)
    server = _Server(("127.0.0.1", port), handler)
    url = f"http://127.0.0.1:{server.server_address[1]}/web/"
    if part is not None:
        url += f"?open=/part/{quote(part.name)}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if open_browser:
        webbrowser.open(url)
    return server, url


def cmd_open(part: str | None, port: int = PORT, open_browser: bool = True) -> int:
    try:
        server, url = serve(Path(part) if part else None, port, open_browser)
    except OSError as err:
        print(f"error: can't start the local page on port {port} ({err}). Is it open already? Try --port {port + 1}.", file=sys.stderr)
        return 1
    print(f"FDM Check is running at {url}\nKeep this window open while you use it; press Ctrl+C to stop.", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()
    return 0


# ---------- setup: the MCP entry for each agent ----------

def mcp_command() -> list[str]:
    """How an agent starts our MCP server: this Python, so it finds this install (pipx / uv tool keep it)."""
    return [sys.executable, "-m", "fdmcore.mcp_server"]


def _codex_block(cmd: list[str]) -> str:
    return (f"\n[mcp_servers.{SERVER_NAME}]\ncommand = {json.dumps(cmd[0])}\n"
            f"args = [{', '.join(json.dumps(a) for a in cmd[1:])}]\n")


def plan(home: Path | None = None, which=None) -> list[dict]:
    """What setup would do for each agent found on this computer: {agent, what, apply()}. Nothing is written here."""
    home, which = home or Path.home(), which or shutil.which
    cmd = mcp_command()
    steps = []
    claude = which("claude")
    if claude:
        steps.append({"agent": "Claude Code", "what": f"claude mcp add --scope user {SERVER_NAME} -- {' '.join(cmd)}",
                      "apply": lambda: subprocess.run([claude, "mcp", "add", "--scope", "user", SERVER_NAME, "--", *cmd], check=True)})
        skills = home / ".claude" / "skills" / "fdm-check"
        steps.append({"agent": "Claude Code", "what": f"copy the guide (how to use the tools) to {skills}",
                      "apply": lambda: shutil.copytree(agents_dir() / "fdm-check", skills, dirs_exist_ok=True)})
    codex = home / ".codex" / "config.toml"
    if codex.parent.is_dir() or which("codex"):
        def add_codex():
            codex.parent.mkdir(parents=True, exist_ok=True)
            text = codex.read_text(encoding="utf-8") if codex.exists() else ""
            if f"[mcp_servers.{SERVER_NAME}]" not in text:
                codex.write_text(text + _codex_block(cmd), encoding="utf-8")
        steps.append({"agent": "Codex", "what": f"add [mcp_servers.{SERVER_NAME}] to {codex}", "apply": add_codex})
    gemini = home / ".gemini" / "settings.json"
    if gemini.parent.is_dir() or which("gemini"):
        def add_gemini():
            gemini.parent.mkdir(parents=True, exist_ok=True)
            data = json.loads(gemini.read_text(encoding="utf-8") or "{}") if gemini.exists() else {}
            data.setdefault("mcpServers", {})[SERVER_NAME] = {"command": cmd[0], "args": cmd[1:]}
            gemini.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        steps.append({"agent": "Gemini CLI", "what": f'add "{SERVER_NAME}" to mcpServers in {gemini}', "apply": add_gemini})
    return steps


def cmd_setup(yes: bool = False, ask=input) -> int:
    steps = plan()
    if not steps:
        print("No AI agent found (Claude Code, Codex or Gemini CLI). Install one, then run `fdmdfm setup` again.\n"
              f"Or add this MCP server by hand: {' '.join(mcp_command())}")
        return 1
    done = 0
    for s in steps:
        if not yes and ask(f"{s['agent']}: {s['what']}? [y/N] ").strip().lower() not in ("y", "yes"):
            print("  skipped")
            continue
        try:
            s["apply"]()
            done += 1
            print("  done")
        except (OSError, ValueError, subprocess.CalledProcessError) as err:
            print(f"  didn't work: {err}", file=sys.stderr)
    if done:
        print("Restart your agent, then ask it to check a part, e.g. \"Check part.stl for printing in PETG\".")
    return 0
