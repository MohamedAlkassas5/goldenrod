"""CLI: load a graph JSON into ClickHouse through the MCP server.

    python -m services.loader data/fixtures/graph-v1.json
    python -m services.loader graph-v1.json --dry-run     # validate + resolve only

Idempotency caveat, worth stating plainly: re-ingesting the SAME revision_id is
idempotent because every graph table's sorting key is the row identity. But
ClickHouse cannot delete on write — so if a graph is edited and re-loaded under
an unchanged revision_id, rows that no longer exist in the new graph are left
behind as orphans. Treat revision_id as immutable: a changed graph gets a new
revision_id. That matches production practice, where a revision is a dated,
coloured set of pages that never changes after it is issued.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from services.common.mcp_client import ClickHouseMCP, MCPError
from services.loader.ingest import IngestError, ingest_graph, resolve, validate_graph


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m services.loader",
        description="Ingest a film graph into ClickHouse through the MCP server.",
    )
    parser.add_argument("graph", type=Path, help="graph JSON (contracts/graph.schema.json)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and resolve identity, but write nothing",
    )
    args = parser.parse_args(argv)

    try:
        graph = json.loads(args.graph.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"could not read {args.graph}: {exc}", file=sys.stderr)
        return 2

    try:
        # Validate and resolve before opening any connection: a bad graph should
        # fail without spawning the MCP server.
        validate_graph(graph)
        rows = resolve(graph)

        if args.dry_run:
            print(f"valid: {args.graph}")
            for table, table_rows in rows.items():
                print(f"  {table:<16} {len(table_rows):>5} rows")
            print("\nnothing written (--dry-run)")
            return 0

        with ClickHouseMCP() as ch:
            print(
                f"MCP: {ch.server_info.get('name')} "
                f"v{ch.server_info.get('version')} tools={ch.tools}"
            )
            result = ingest_graph(graph, ch)

        print(f"ingested {result['production_id']} @ {result['revision_id']}")
        for table, count in result["written"].items():
            print(f"  {table:<16} {count:>5} rows")
        print("identity verified against the database's MATERIALIZED fact_key")
        return 0

    except (IngestError, MCPError) as exc:
        print(f"\nINGEST FAILED\n{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
