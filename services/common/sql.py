"""SQL literal rendering for statements sent through the MCP `run_query` tool.

The MCP tool takes a single SQL string, so values are rendered into the
statement here rather than bound by a driver. Everything user- or
model-supplied (synopses, quotes, statements) goes through `lit()`, which is the
only place quoting is allowed to happen.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

# ClickHouse string literals are single-quoted with backslash escapes.
_ESCAPES = str.maketrans({
    "\\": "\\\\",
    "'": "\\'",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\0": "\\0",
})


def lit(value: Any) -> str:
    """Render a Python value as a ClickHouse SQL literal."""
    if value is None:
        return "''"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, datetime):
        return f"'{value:%Y-%m-%d %H:%M:%S}'"
    if isinstance(value, date):
        return f"'{value:%Y-%m-%d}'"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(lit(v) for v in value) + "]"
    return "'" + str(value).translate(_ESCAPES) + "'"


def ident(name: str) -> str:
    """Render an identifier. Rejects anything that is not a plain name."""
    if not name.replace("_", "").replace(".", "").isalnum():
        raise ValueError(f"unsafe identifier: {name!r}")
    return name


def split_statements(sql_text: str) -> list[str]:
    """Split a .sql file into executable statements.

    The MCP `run_query` tool takes one statement at a time. Comment lines are
    dropped first, so a statement preceded by a comment block is not mistaken for
    a comment — a naive `text.split(';')` gets that wrong and silently skips DDL.
    """
    statements: list[str] = []
    buffer: list[str] = []
    for line in sql_text.splitlines():
        if line.strip().startswith("--"):
            continue
        buffer.append(line)
        if line.rstrip().endswith(";"):
            statement = "\n".join(buffer).strip().rstrip(";").strip()
            if statement:
                statements.append(statement)
            buffer = []
    tail = "\n".join(buffer).strip().rstrip(";").strip()
    if tail:
        statements.append(tail)
    return statements


def insert(table: str, columns: list[str], rows: list[dict[str, Any]]) -> str:
    """Build a multi-row INSERT. Returns '' when there is nothing to write."""
    if not rows:
        return ""
    cols = ", ".join(ident(c) for c in columns)
    values = ",\n  ".join(
        "(" + ", ".join(lit(row.get(c)) for c in columns) + ")" for row in rows
    )
    return f"INSERT INTO {ident(table)} ({cols}) VALUES\n  {values}"
