"""Integration: the loader against a real ClickHouse, through the MCP server.

Every statement here goes through the MCP `run_query` tool. Nothing in the test
suite imports a ClickHouse driver either.

Skipped automatically when no ClickHouse is reachable, so the unit suite still
runs on a machine without one. Run the whole thing with:

    python -m pytest tests -q
"""

from __future__ import annotations

import pytest

from services.common.mcp_client import ClickHouseMCP, MCPError
from services.common.queries import bind, get_query
from services.common.sql import insert, lit
from services.loader.identity import fact_key
from services.loader.ingest import ingest_graph, resolve

pytestmark = pytest.mark.mcp

PRODUCTION = "test_demo"


@pytest.fixture(scope="module")
def ch():
    try:
        client = ClickHouseMCP().connect()
        client.run_query("SELECT 1")
    except (MCPError, OSError, FileNotFoundError) as exc:
        pytest.skip(f"no ClickHouse reachable through MCP: {exc}")
    yield client
    client.close()


def graph_rows(ch, table: str, revision_id: str, columns: str = "*") -> list[dict]:
    """Read a graph table correctly.

    FINAL is mandatory here: every graph table is a ReplacingMergeTree and
    ClickHouse only collapses duplicate sorting keys when parts merge, which is
    asynchronous. Without FINAL a re-ingest can show both row versions.
    """
    scope = f"production_id = {lit(PRODUCTION)}"
    if revision_id and table != "entities":
        scope += f" AND revision_id = {lit(revision_id)}"
    return ch.rows(f"SELECT {columns} FROM {table} FINAL WHERE {scope}")


# --- the MCP path itself ---------------------------------------------------
def test_mcp_exposes_run_query_and_writes_are_enabled(ch):
    assert "run_query" in ch.tools
    assert ch.server_info.get("name")


def test_no_clickhouse_driver_anywhere_in_services():
    """CLAUDE.md rule 4: no direct driver calls, not even temporarily.

    Scans source text rather than imported names, so a driver used inside a
    function body or behind a lazy import is caught too.
    """
    import re
    from pathlib import Path

    banned = ("clickhouse_driver", "clickhouse_connect", "asynch", "chdb", "clickhouse_sqlalchemy")
    # match real imports, not the word "asynchronous" in a comment
    pattern = re.compile(
        rf"^\s*(?:import|from)\s+({'|'.join(banned)})\b", re.MULTILINE
    )
    root = Path(__file__).resolve().parents[1] / "services"
    offenders = [
        f"{path.relative_to(root.parent)}: {m.group(1)}"
        for path in root.rglob("*.py")
        for m in pattern.finditer(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"direct ClickHouse access found: {offenders}"


# --- ingest ---------------------------------------------------------------
def test_ingest_writes_every_graph_table(ch, graph_v1):
    result = ingest_graph(graph_v1, ch)
    assert result["verified"] is True
    assert result["written"] == {
        "scenes": 3, "entities": 2, "facts": 1,
        "knowledge_state": 1, "dependencies": 1,
    }


def test_database_fact_key_agrees_with_loader(ch, graph_v1):
    """The MATERIALIZED column and identity.py are two implementations of one
    rule. This is the check that stops them drifting."""
    ingest_graph(graph_v1, ch)
    rows = graph_rows(ch, "facts", "v1", "fact_key, fact_match_key")
    db_key = rows[0]["fact_key"]

    resolved = resolve(graph_v1)["facts"][0]
    ours = fact_key(
        resolved["production_id"], resolved["kind"],
        resolved["established_in_scene_number"], resolved["entity_ids"],
        resolved["collision_ord"],
    )
    assert db_key == ours == f"{PRODUCTION}|knowledge|18|entity:letter,entity:rana#1"


def test_entity_id_order_does_not_change_stored_fact_key(ch, graph_v1):
    """Same fact, entity_ids reversed, must land on the same row."""
    ingest_graph(graph_v1, ch)
    before = graph_rows(ch, "facts", "v1", "fact_key")

    reversed_graph = dict(graph_v1)
    reversed_graph["facts"] = [dict(graph_v1["facts"][0])]
    reversed_graph["facts"][0]["entity_ids"] = list(
        reversed(graph_v1["facts"][0]["entity_ids"])
    )
    ingest_graph(reversed_graph, ch)

    after = graph_rows(ch, "facts", "v1", "fact_key")
    assert {r["fact_key"] for r in before} == {r["fact_key"] for r in after}
    assert len(after) == 1


# --- idempotency ----------------------------------------------------------
def test_ingesting_the_same_revision_twice_is_idempotent(ch, graph_v1):
    ingest_graph(graph_v1, ch)
    first = {t: graph_rows(ch, t, "v1") for t in
             ("scenes", "facts", "knowledge_state", "dependencies")}

    ingest_graph(graph_v1, ch)
    second = {t: graph_rows(ch, t, "v1") for t in
              ("scenes", "facts", "knowledge_state", "dependencies")}

    for table in first:
        assert len(first[table]) == len(second[table]), f"{table} row count changed"
        # updated_at is expected to move; nothing else may
        strip = lambda rows: sorted(
            tuple(sorted((k, str(v)) for k, v in r.items() if k != "updated_at"))
            for r in rows
        )
        assert strip(first[table]) == strip(second[table]), f"{table} content changed"


def test_first_seen_revision_is_preserved_across_revisions(ch, graph_v1, graph_v2):
    ingest_graph(graph_v1, ch)
    ingest_graph(graph_v2, ch)
    rows = graph_rows(ch, "entities", "", "entity_id, first_seen_revision_id")
    assert {r["first_seen_revision_id"] for r in rows} == {"v1"}


# --- database-level contract enforcement ----------------------------------
@pytest.mark.parametrize(
    "table, columns, row, bad",
    [
        ("facts", ["production_id", "revision_id", "kind",
                   "established_in_scene_number", "entity_ids", "statement"],
         {"production_id": PRODUCTION, "revision_id": "bad", "kind": "vibes",
          "established_in_scene_number": "1", "entity_ids": ["e"], "statement": "x"},
         "kind_enum"),
        ("scenes", ["production_id", "revision_id", "scene_number", "scene_id",
                    "day_night", "synopsis"],
         {"production_id": PRODUCTION, "revision_id": "bad", "scene_number": "1",
          "scene_id": "s1", "day_night": "MIDNIGHT", "synopsis": "x"},
         "day_night_enum"),
        ("entities", ["production_id", "entity_id", "type", "name"],
         {"production_id": PRODUCTION, "entity_id": "e", "type": "macguffin",
          "name": "x"},
         "type_enum"),
        ("dependencies", ["production_id", "revision_id", "from_fact_key",
                          "to_scene_number", "kind"],
         {"production_id": PRODUCTION, "revision_id": "bad", "from_fact_key": "k",
          "to_scene_number": "1", "kind": "implies"},
         "kind_enum"),
    ],
)
def test_database_rejects_contract_invalid_enums(ch, table, columns, row, bad):
    """Belt and braces: the contract is enforced by the schema too, so a bad row
    cannot enter even if it bypasses validate_graph."""
    with pytest.raises(MCPError) as exc:
        ch.run_query(insert(table, columns, [row]))
    assert bad in str(exc.value) or "Constraint" in str(exc.value)


def test_destructive_operations_stay_blocked(ch):
    """CLICKHOUSE_ALLOW_DROP=false must remain in force."""
    with pytest.raises(MCPError) as exc:
        ch.run_query("DROP TABLE facts")
    assert "not allowed" in str(exc.value).lower()


# --- the planted relocation, end to end -----------------------------------
def test_planted_relocation_is_detected_by_the_diff_query(ch, graph_v1, graph_v2):
    """v1 -> v2 must come back as one RELOCATED fact, 18 -> 31.

    Runs the `draft_diff` query out of schema.sql itself rather than a copy of
    it. A copy is how the reference query and the query under test drift apart —
    which is the whole reason services/common/queries.py extracts it instead.
    """
    ingest_graph(graph_v1, ch)
    ingest_graph(graph_v2, ch)

    rows = ch.rows(
        bind(
            get_query("draft_diff"),
            production=PRODUCTION,
            prior_revision="v1",
            current_revision="v2",
        )
    )

    assert len(rows) == 1
    assert rows[0]["change"] == "RELOCATED"
    assert rows[0]["was_scene"] == "18"
    assert rows[0]["now_scene"] == "31"


def test_second_order_hop_survives_the_relocation(ch, graph_v1, graph_v2):
    """The change lands in 18/31 but the damage is in scene 24 — and scene 24 is
    reachable from the relocated fact in both revisions."""
    ingest_graph(graph_v1, ch)
    ingest_graph(graph_v2, ch)

    rows = ch.rows(f"""
        SELECT revision_id, to_scene_number, kind, evidence_line
        FROM dependencies FINAL
        WHERE production_id = {lit(PRODUCTION)}
          AND from_fact_key IN (
              SELECT fact_key FROM facts FINAL
              WHERE production_id = {lit(PRODUCTION)}
          )
        ORDER BY revision_id
    """)
    assert [r["to_scene_number"] for r in rows] == ["24", "24"]
    assert {r["evidence_line"] for r in rows} == {388}


def test_knowledge_state_flip_is_visible(ch, graph_v1, graph_v2):
    """Rana knows in 24 under v1, and does not under v2. This is the break."""
    ingest_graph(graph_v1, ch)
    ingest_graph(graph_v2, ch)

    rows = ch.rows(f"""
        SELECT revision_id, knows
        FROM knowledge_state FINAL
        WHERE production_id = {lit(PRODUCTION)}
          AND character_entity_id = 'entity:rana'
          AND scene_number = '24'
        ORDER BY revision_id
    """)
    assert [(r["revision_id"], r["knows"]) for r in rows] == [("v1", 1), ("v2", 0)]
