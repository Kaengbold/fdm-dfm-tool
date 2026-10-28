"""Repair common YAML quoting mistakes in LLM-generated research files (in place).

Plain scalar values that contain ": ", " #", or start with a backtick/@/% are wrapped in
double quotes. Text is not changed otherwise. Usage: python tools/fix_research_yaml.py research/sourced_*.yaml
"""

import re
import sys

import yaml

LINE = re.compile(r"^(\s*(?:- )?[A-Za-z_][\w-]*:[ \t]+)(.+?)\s*$")
ITEM = re.compile(r"^(\s*- )(?![\w-]+:\s)(.+?)\s*$")  # bare list item like "- Author — Title — URL"


def needs_quotes(v: str) -> bool:
    if v[0] in "\"'":
        # Looks quoted, but text after the closing quote (or inner quotes) breaks it.
        try:
            yaml.safe_load("k: " + v)
            return False
        except yaml.YAMLError:
            return True
    if v[0] in "{[|>&*!":
        return False
    return ": " in v or " #" in v or v[0] in "`@%" or v.endswith(":")


def quote(v: str) -> str:
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def fix(text: str) -> str:
    out = []
    for line in text.splitlines():
        m = LINE.match(line) or ITEM.match(line)
        if m and needs_quotes(m.group(2)):
            line = m.group(1) + quote(m.group(2))
        out.append(line)
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    for path in sys.argv[1:]:
        raw = open(path, encoding="utf-8").read()
        fixed = fix(raw)
        try:
            yaml.safe_load(fixed)
            status = "ok"
        except yaml.YAMLError as e:
            status = f"STILL BROKEN: {str(e).splitlines()[0]} ({e.problem_mark})"
        if fixed != raw:
            open(path, "w", encoding="utf-8").write(fixed)
        print(f"{path}: {status}")
