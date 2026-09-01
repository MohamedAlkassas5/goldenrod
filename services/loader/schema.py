"""Apply db/clickhouse/schema.sql through the ClickHouse MCP server.

    python -m services.loader.schema              # apply
    python -m services.loader.schema --check      # report what exists, write nothing

Every statement goes through the MCP `run_query` tool. The schema is written with
CREATE ... IF NOT EXISTS throughout, so applying it repeatedly is safe.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from services.common.mcp_client import ClickHouseMCP, MCPError
from services.common.sql import split_statements

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA = REPO_ROOT / "db" / "clickhouse" / "schema.sql"

EXPECTED_TABLES = (
    "scenes", "entities", "facts", "knowledge_state", "dependencies",
    "decisions", "commitments", "findings",
    "access_grants", "access_log",
)


def _label(statement: str) -> str:
    words = statement.split()
    upper = statement.upper()
    if upper.startswith("CREATE TABLE"):
        return f"table {words[5]}"
    if "FUNCTION" in upper:
        return f"function {words[words.index('FUNCTION') + 1]}"
    if upper.startswith("CREATE DATABASE"):
        return f"database {words[-1]}"
    return " ".join(words[:4])


def create_database(database: str = "goldenrod") -> None:
    """Create the database, from a client connected to `default`.

    The chicken-and-egg this exists to break: the MCP server selects
    CLICKHOUSE_DATABASE when it connects, so a client configured for `goldenrod`
    cannot be the one that creates `goldenrod` — on a database that does not
    exist yet, every statement including `CREATE DATABASE` fails with
    UNKNOWN_DATABASE. It went unnoticed locally because the database was already
    there; on a fresh ClickHouse Cloud service it is the first thing that
    happens, and it stopped the run instructions dead.

    `default` is guaranteed to exist on any ClickHouse, hosted or local.
    """
    with ClickHouseMCP(env={"CLICKHOUSE_DATABASE": "default"}) as bootstrap:
        bootstrap.run_query(f"CREATE DATABASE IF NOT EXISTS {database}")


def apply_schema(ch: ClickHouseMCP, database: str = "goldenrod") -> list[str]:
    applied = []
    for statement in split_statements(SCHEMA.read_text(encoding="utf-8")):
        ch.run_query(statement)
        applied.append(_label(statement))
    return applied


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m services.loader.schema")
    parser.add_argument("--database", default="goldenrod")
    parser.add_argument("--check", action="store_true", help="report state, write nothing")
    args = parser.parse_args(argv)

    try:
        if not args.check:
            create_database(args.database)
        with ClickHouseMCP() as ch:
            if args.check:
                rows = ch.rows(
                    f"SELECT name, engine FROM system.tables "
                    f"WHERE database = '{args.database}' ORDER BY name"
                )
                found = {r["name"] for r in rows}
                for name in EXPECTED_TABLES:
                    mark = "ok     " if name in found else "MISSING"
                    engine = next((r["engine"] for r in rows if r["name"] == name), "-")
                    print(f"  {mark} {name:<16} {engine}")
                missing = [t for t in EXPECTED_TABLES if t not in found]
                return 1 if missing else 0

            for label in apply_schema(ch, args.database):
                print(f"  applied {label}")
            print(f"\nschema applied to {args.database}")
        return 0
    except MCPError as exc:
        print(f"\nSCHEMA APPLY FAILED\n{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
