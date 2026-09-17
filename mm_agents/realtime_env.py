"""Load realtime run settings from the repository-level ``.env`` file.

The realtime runners need a gateway URL, one API key per model and an optional
proxy.  Typing them on every command line is error prone, so a git-ignored
``.env`` next to this repository is read once per process:

    ANTHROPIC_BASE_URL=https://gateway.example.com/anthropic
    PACKY_COMMON_API_KEY=sk-...

Rules, chosen so that an existing shell environment always wins:

* only ``KEY=value`` lines are read; ``export KEY=value`` is accepted;
* ``#`` starts a comment unless it is inside quotes; blank lines are ignored;
* surrounding single or double quotes are removed;
* a variable already present in ``os.environ`` is never overwritten, and
  ``override=True`` is only for tests that deliberately want that.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = REPO_ROOT / ".env"
NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        body = value[1:-1]
        if value[0] == '"':
            body = (body.replace("\\n", "\n").replace("\\t", "\t")
                        .replace('\\"', '"').replace("\\\\", "\\"))
        return body
    return value


def _strip_inline_comment(value: str) -> str:
    """Drop a trailing ``# comment`` that is outside quotes and space separated."""
    quote = None
    for index, char in enumerate(value):
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "#" and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
    return value


def parse_env_text(text: str) -> dict:
    """Turn ``.env`` content into a mapping, ignoring comments and blank lines."""
    values = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            raise ValueError(f"{DEFAULT_ENV_FILE.name} line {number}: expected KEY=value")
        name, _, value = line.partition("=")
        name = name.strip()
        if not NAME_RE.fullmatch(name):
            raise ValueError(f"{DEFAULT_ENV_FILE.name} line {number}: invalid variable name {name!r}")
        value = _unquote(_strip_inline_comment(value.strip()).strip())
        values[name] = value
    return values


def load_env_file(path=None, *, override=False, environ=None) -> dict:
    """Apply ``path`` (default ``<repo>/.env``) to the environment.

    Returns the variables this call actually set, so callers can report the
    file as a key source without ever printing a secret.
    """
    target = Path(path) if path else DEFAULT_ENV_FILE
    if not target.exists():
        return {}
    environ = os.environ if environ is None else environ
    applied = {}
    for name, value in parse_env_text(target.read_text(encoding="utf-8")).items():
        if not value:
            continue
        if name in environ and not override:
            continue
        environ[name] = value
        applied[name] = value
    return applied


def describe_sources(applied, path=None) -> str:
    """One line naming where configuration came from, never its values."""
    target = Path(path) if path else DEFAULT_ENV_FILE
    if not applied:
        return f"no values loaded from {target} (missing file or all names already set)"
    return f"loaded {len(applied)} value(s) from {target}: " + ", ".join(sorted(applied))
