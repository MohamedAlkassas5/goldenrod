"""The project's canonical SQL, read out of db/clickhouse/schema.sql.

schema.sql is the single source of truth for the analytical queries — it is the
file a judge reads, and the one the demo puts on screen. Copying that SQL into
Python would create a second implementation that silently drifts, so the queries
are extracted from it instead, delimited by markers:

    -- >>> QUERY commitment_ranking
    -- ... commented SQL ...
    -- <<< QUERY

Ranking in particular has exactly one implementation: the `commitmentRank` UDF
plus the `commitment_ranking` query below. Do not write another one.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA = REPO_ROOT / "db" / "clickhouse" / "schema.sql"

_BLOCK = re.compile(
    r"^--\s*>>>\s*QUERY\s+(?P<name>\w+)\s*$(?P<body>.*?)^--\s*<<<\s*QUERY\s*$",
    re.MULTILINE | re.DOTALL,
)


def _uncomment(body: str) -> str:
    lines = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("--"):
            raise ValueError(f"non-comment line inside a QUERY block: {line!r}")
        lines.append(re.sub(r"^--\s?", "", stripped))
    return "\n".join(lines).strip()


def load_queries(schema_path: Path | None = None) -> dict[str, str]:
    """Every named query in schema.sql, uncommented and ready to run."""
    text = (schema_path or SCHEMA).read_text(encoding="utf-8")
    return {m.group("name"): _uncomment(m.group("body")) for m in _BLOCK.finditer(text)}


def get_query(name: str, schema_path: Path | None = None) -> str:
    queries = load_queries(schema_path)
    if name not in queries:
        raise KeyError(f"no query {name!r} in {SCHEMA}. Found: {sorted(queries)}")
    return queries[name]


def bind(sql: str, **params: str) -> str:
    """Substitute ClickHouse-style {name:Type} placeholders with SQL literals.

    The MCP `run_query` tool takes a single SQL string and has nowhere to put
    bound parameters, so the placeholders in schema.sql are filled here. Values
    go through `lit()`, so quoting happens in exactly one place.
    """
    from services.common.sql import lit

    def replace(match: re.Match) -> str:
        name = match.group(1)
        if name not in params:
            raise KeyError(f"missing parameter {name!r} for query")
        return lit(params[name])

    filled = re.sub(r"\{(\w+):[A-Za-z0-9()]+\}", replace, sql)
    leftover = re.findall(r"\{(\w+):", filled)
    if leftover:
        raise KeyError(f"unfilled parameters: {sorted(set(leftover))}")
    return filled
