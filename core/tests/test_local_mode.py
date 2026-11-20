"""Local mode without a checkout (T-450): `fdmdfm open` serves the app and the part, `fdmdfm setup` writes the MCP
entries (only after a yes)."""

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from fdmcore import local

SAMPLE = Path(__file__).resolve().parents[2] / "web" / "samples" / "hook.stl"


def _get(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as err:
        return err.code, b""


def test_open_serves_the_app_the_engine_and_only_that_part():
    server, url = local.serve(SAMPLE, port=0, open_browser=False)
    try:
        base = url.split("/web/")[0]
        assert url.endswith("/web/?open=/part/hook.stl")
        status, page = _get(url)
        assert status == 200 and b"<html" in page.lower()
        assert _get(base + "/core/fdmcore/manifest.json")[0] == 200  # where the app loads the engine from
        assert _get(base + "/part/hook.stl") == (200, SAMPLE.read_bytes())
        for other in ("/part/other.stl", "/core/fdmcore/../../README.md", "/web/../core/pyproject.toml", "/README.md"):
            assert _get(base + other)[0] == 404, other
    finally:
        server.shutdown()


def test_setup_writes_each_agent_only_after_a_yes(tmp_path):
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".gemini").mkdir()
    (tmp_path / ".gemini" / "settings.json").write_text('{"theme": "dark", "mcpServers": {"other": {"command": "x"}}}')
    steps = local.plan(home=tmp_path, which=lambda name: None)  # no `claude` on this computer
    assert [s["agent"] for s in steps] == ["Codex", "Gemini CLI"]
    assert not (tmp_path / ".codex" / "config.toml").exists()  # planning writes nothing
    for s in steps:
        s["apply"]()
        s["apply"]()  # twice: still one entry
    codex = (tmp_path / ".codex" / "config.toml").read_text()
    assert codex.count("[mcp_servers.fdm-dfm]") == 1 and '"-m", "fdmcore.mcp_server"' in codex
    gemini = json.loads((tmp_path / ".gemini" / "settings.json").read_text())
    assert gemini["theme"] == "dark" and set(gemini["mcpServers"]) == {"other", "fdm-dfm"}
    assert gemini["mcpServers"]["fdm-dfm"]["args"] == ["-m", "fdmcore.mcp_server"]


def test_setup_asks_and_a_no_changes_nothing(tmp_path, monkeypatch):
    (tmp_path / ".codex").mkdir()
    monkeypatch.setattr(local.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(local.shutil, "which", lambda name: None)
    asked = []
    assert local.cmd_setup(ask=lambda q: asked.append(q) or "n") == 0
    assert len(asked) == 1 and "Codex" in asked[0] and not (tmp_path / ".codex" / "config.toml").exists()


def test_claude_code_gets_the_server_and_the_guide(tmp_path):
    steps = local.plan(home=tmp_path, which=lambda name: "claude" if name == "claude" else None)
    claude = [s for s in steps if s["agent"] == "Claude Code"]
    assert "claude mcp add --scope user fdm-dfm --" in claude[0]["what"]
    claude[1]["apply"]()
    assert (tmp_path / ".claude" / "skills" / "fdm-check" / "SKILL.md").is_file()


def test_open_says_so_when_the_port_is_taken():
    server, url = local.serve(None, port=0, open_browser=False)
    try:
        port = int(url.split(":")[2].split("/")[0])
        with pytest.raises(OSError):
            local.serve(None, port=port, open_browser=False)
    finally:
        server.shutdown()
