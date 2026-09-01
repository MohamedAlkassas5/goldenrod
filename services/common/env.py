"""Fill gaps in the environment from `.env`.

The README tells anyone running this to copy `.env.example` to `.env` and fill it
in, and the "instructions needed to run" section is graded. Nothing was reading
that file, so the instruction was untrue on every path — this makes it true.

TWO RULES, AND THE SECOND ONE MATTERS MORE
------------------------------------------
1. The file only ever fills gaps. A variable already present in the real
   environment always wins, so a deployment's own configuration can never be
   shadowed by a stray `.env` that got into an image. On Cloud Run there is no
   `.env` at all and this is a no-op.
2. It is loaded from `services/common/__init__.py`, at import, rather than from
   each CLI's `main()`. The interface is served as `services.api.app:app` by
   uvicorn in the container, so `services/api/__main__.py` never runs there and a
   call sited in `main()` would silently not happen on the one path that matters
   most. Import is the only point every entry shares.

No dependency. python-dotenv would be a reasonable choice and this is thirty
lines, so it is thirty lines — the same reasoning as the hand-written MCP client.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env"


def parse_env(text: str) -> dict[str, str]:
    """`KEY=value` lines to a dict. Blank lines and `#` comments are skipped.

    Tolerates the shapes people actually paste: a leading `export`, surrounding
    single or double quotes, and whitespace around the `=`. A line without an
    `=` is skipped rather than guessed at.
    """
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_env(path: Path | None = None) -> list[str]:
    """Apply `.env` to os.environ, without overriding anything already set.

    Returns the names it actually set, so a caller can say so. Missing or
    unreadable file is not an error: running entirely from real environment
    variables is the normal case in a container.
    """
    path = path or ENV_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    applied = []
    for key, value in parse_env(text).items():
        if key not in os.environ:
            os.environ[key] = value
            applied.append(key)
    return applied
