"""The page's Content Security Policy allows its inline import map by hash: once as served from the
repo, once as the self-hosted build rewrites it (pages.yml). An edited map without new hashes would
leave the app without three.js, so check both here (the browser test only sees the first)."""

import base64
import hashlib
import re
from pathlib import Path

PAGE = Path(__file__).resolve().parents[2] / "web" / "index.html"


def _hash(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'"


def test_csp_allows_the_import_map_in_both_builds():
    html = PAGE.read_text(encoding="utf-8")
    csp = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', html).group(1)
    importmap = re.search(r'<script type="importmap">(.*?)</script>', html, re.S).group(1)
    assert "\n" not in importmap  # one line: a CRLF checkout must not change the hash
    assert _hash(importmap) in csp
    assert _hash(importmap.replace("https://cdn.jsdelivr.net", "../vendor")) in csp
    assert "object-src 'none'" in csp
