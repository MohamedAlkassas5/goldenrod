"""Graph ingest: contract-valid graph JSON in, ClickHouse rows out.

Pipeline, in order, and it fails loudly at every step rather than writing a
partly-broken graph:

    1. validate            graph JSON against contracts/graph.schema.json
    2. resolve             scene_id -> scene_number, fact_id -> fact_key
    3. number collisions   deterministically, by source_line
    4. write               through the MCP run_query tool only
    5. verify              the database's MATERIALIZED fact_key agrees with ours,
                           and every knowledge_state / dependency resolves to a
                           fact that exists

Reading graph data afterwards: see READ_SEMANTICS below. Every graph table is a
ReplacingMergeTree, so reads need FINAL.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from services.common.mcp_client import ClickHouseMCP
from services.common.sql import insert
from services.loader.identity import (
    assign_collision_ords,
    fact_key,
    normalise_entity_ids,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
GRAPH_SCHEMA = REPO_ROOT / "contracts" / "graph.schema.json"

INSERT_BATCH = 500

# ---------------------------------------------------------------------------
# READ SEMANTICS — required reading before querying any graph table.
#
# scenes, entities, facts, knowledge_state and dependencies are all
# ReplacingMergeTree. ClickHouse only collapses duplicate sorting keys when parts
# merge, and merges are asynchronous with no guaranteed timing. A plain SELECT
# can therefore return BOTH the old and the new version of a row — for an
# unbounded period after a re-ingest.
#
# So every read of a graph table must collapse duplicates explicitly, by one of:
#
#     SELECT ... FROM facts FINAL WHERE production_id = ... AND revision_id = ...
#
#     SELECT <sorting key>, argMax(col, updated_at) AS col
#     FROM facts GROUP BY <sorting key>
#
# FINAL is the simpler choice at our data volume and is what this module uses.
# argMax is preferable inside a larger aggregate query, which is why the demo
# query in schema.sql collapses `commitments` that way before joining.
#
# The append-only tables are the exception: `decisions` and `findings` are plain
# MergeTree and must NOT be read with FINAL — every row there is a distinct
# historical event, and collapsing them would silently discard ledger history.
# ---------------------------------------------------------------------------
READ_SEMANTICS = "graph tables are ReplacingMergeTree; read with FINAL"

GRAPH_TABLES = ("scenes", "entities", "facts", "knowledge_state", "dependencies")
APPEND_ONLY_TABLES = ("decisions", "findings", "commitments")


class IngestError(RuntimeError):
    """Ingest refused the graph. Nothing was written, or the write is suspect."""


# --- 1. validate -----------------------------------------------------------
def validate_graph(graph: dict[str, Any]) -> None:
    """Validate against contracts/graph.schema.json. Raises with every error."""
    schema = json.loads(GRAPH_SCHEMA.read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(schema).iter_errors(graph), key=lambda e: list(e.path)
    )
    if errors:
        detail = "\n".join(
            f"  {'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}"
            for e in errors[:25]
        )
        more = f"\n  ... and {len(errors) - 25} more" if len(errors) > 25 else ""
        raise IngestError(
            f"graph failed contracts/graph.schema.json ({len(errors)} errors):\n{detail}{more}"
        )


# --- 2 + 3. resolve and number --------------------------------------------
def resolve(graph: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Turn contract-shaped graph JSON into row dicts ready for INSERT.

    Resolves the passthrough handles (scene_id, fact_id) onto the stable
    coordinates (scene_number, fact_key) and numbers identity collisions.
    """
    production_id = graph["production_id"]
    revision_id = graph["revision_id"]

    scene_number_by_id: dict[str, str] = {}
    for scene in graph["scenes"]:
        scene_number_by_id[scene["scene_id"]] = scene["scene_number"]

    def scene_number(scene_id: str, where: str) -> str:
        if scene_id not in scene_number_by_id:
            raise IngestError(
                f"{where} references scene_id {scene_id!r}, which is not in this "
                f"graph's `scenes`. ClickHouse has no foreign keys, so referential "
                f"integrity is enforced here or not at all."
            )
        return scene_number_by_id[scene_id]

    # -- scenes
    scene_rows = [
        {
            "production_id": production_id,
            "revision_id": revision_id,
            "scene_number": s["scene_number"],
            "scene_id": s["scene_id"],
            "int_ext": s.get("int_ext", ""),
            "location_id": s.get("location_id", ""),
            "day_night": s.get("day_night", ""),
            "page_eighths": int(s.get("page_eighths") or 0),
            "synopsis": s["synopsis"],
            "text_hash": s.get("text_hash", ""),
            "entity_ids": normalise_entity_ids(s.get("entity_ids")),
        }
        for s in graph["scenes"]
    ]

    # -- entities (production-scoped, not revision-scoped)
    entity_rows = [
        {
            "production_id": production_id,
            "entity_id": e["entity_id"],
            "type": e["type"],
            "name": e["name"],
            "aliases": sorted(set(e.get("aliases") or [])),
            "first_seen_revision_id": revision_id,
        }
        for e in graph["entities"]
    ]

    # -- facts: normalise, resolve scene, then number collisions
    staged: list[dict[str, Any]] = []
    for f in graph["facts"]:
        staged.append(
            {
                "production_id": production_id,
                "revision_id": revision_id,
                "kind": f["kind"],
                "established_in_scene_number": scene_number(
                    f["established_in_scene_id"], f"fact {f['fact_id']!r}"
                ),
                "entity_ids": normalise_entity_ids(f.get("entity_ids")),
                "statement": f["statement"],
                "fact_id": f["fact_id"],
                "established_in_scene_id": f["established_in_scene_id"],
                "source_line": int(f.get("source_line") or 0),
            }
        )

    ords = assign_collision_ords(staged)
    fact_key_by_fact_id: dict[str, str] = {}
    fact_rows: list[dict[str, Any]] = []
    for row, collision_ord in zip(staged, ords):
        row["collision_ord"] = collision_ord
        key = fact_key(
            row["production_id"],
            row["kind"],
            row["established_in_scene_number"],
            row["entity_ids"],
            collision_ord,
        )
        if row["fact_id"] in fact_key_by_fact_id:
            raise IngestError(
                f"duplicate fact_id {row['fact_id']!r} in this revision; fact_id is a "
                f"passthrough handle but must still be unique within one graph."
            )
        fact_key_by_fact_id[row["fact_id"]] = key
        fact_rows.append(row)

    def resolve_fact(fact_id: str, where: str) -> str:
        if fact_id not in fact_key_by_fact_id:
            raise IngestError(
                f"{where} references fact_id {fact_id!r}, which is not in this "
                f"graph's `facts`."
            )
        return fact_key_by_fact_id[fact_id]

    # -- knowledge_state: identity derives from fact_key
    knowledge_rows = [
        {
            "production_id": production_id,
            "revision_id": revision_id,
            "character_entity_id": k["character_entity_id"],
            "fact_key": resolve_fact(k["fact_id"], "knowledge_state entry"),
            "scene_number": scene_number(k["scene_id"], "knowledge_state entry"),
            "knows": 1 if k["knows"] else 0,
            "acquired_via": k.get("acquired_via", ""),
            "fact_id": k["fact_id"],
            "scene_id": k["scene_id"],
        }
        for k in graph.get("knowledge_state", [])
    ]

    # -- dependencies: identity derives from fact_key
    dependency_rows = [
        {
            "production_id": production_id,
            "revision_id": revision_id,
            "from_fact_key": resolve_fact(d["from_fact_id"], "dependency"),
            "to_scene_number": scene_number(d["to_scene_id"], "dependency"),
            "kind": d["kind"],
            "evidence_line": int(d.get("evidence_line") or 0),
            "evidence_quote": d.get("evidence_quote", ""),
            "dependency_id": d["dependency_id"],
            "from_fact_id": d["from_fact_id"],
            "to_scene_id": d["to_scene_id"],
        }
        for d in graph.get("dependencies", [])
    ]

    return {
        "scenes": scene_rows,
        "entities": entity_rows,
        "facts": fact_rows,
        "knowledge_state": knowledge_rows,
        "dependencies": dependency_rows,
    }


# --- 4. write --------------------------------------------------------------
COLUMNS: dict[str, list[str]] = {
    "scenes": [
        "production_id", "revision_id", "scene_number", "scene_id", "int_ext",
        "location_id", "day_night", "page_eighths", "synopsis", "text_hash",
        "entity_ids", "updated_at",
    ],
    "entities": [
        "production_id", "entity_id", "type", "name", "aliases",
        "first_seen_revision_id", "updated_at",
    ],
    "facts": [
        "production_id", "revision_id", "kind", "established_in_scene_number",
        "entity_ids", "collision_ord", "statement", "fact_id",
        "established_in_scene_id", "source_line", "updated_at",
    ],
    "knowledge_state": [
        "production_id", "revision_id", "character_entity_id", "fact_key",
        "scene_number", "knows", "acquired_via", "fact_id", "scene_id", "updated_at",
    ],
    "dependencies": [
        "production_id", "revision_id", "from_fact_key", "to_scene_number", "kind",
        "evidence_line", "evidence_quote", "dependency_id", "from_fact_id",
        "to_scene_id", "updated_at",
    ],
}


def _preserve_first_seen(
    ch: ClickHouseMCP, production_id: str, entity_rows: list[dict[str, Any]]
) -> None:
    """Keep the original first_seen_revision_id for entities we already have.

    entities is production-scoped and ReplacingMergeTree keeps the newest row, so
    a re-ingest would otherwise overwrite first_seen with the current revision.
    """
    if not entity_rows:
        return
    existing = {
        r["entity_id"]: r["first_seen_revision_id"]
        for r in ch.rows(
            f"SELECT entity_id, first_seen_revision_id FROM entities FINAL "
            f"WHERE production_id = {_q(production_id)}"
        )
    }
    for row in entity_rows:
        was = existing.get(row["entity_id"])
        if was:
            row["first_seen_revision_id"] = was


def _q(value: str) -> str:
    from services.common.sql import lit

    return lit(value)


def write(ch: ClickHouseMCP, rows: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    """Write every table through the MCP run_query tool. Returns rows per table."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    written: dict[str, int] = {}
    for table in GRAPH_TABLES:
        table_rows = rows.get(table, [])
        for row in table_rows:
            row.setdefault("updated_at", now)
        for start in range(0, len(table_rows), INSERT_BATCH):
            batch = table_rows[start : start + INSERT_BATCH]
            sql = insert(table, COLUMNS[table], batch)
            if sql:
                ch.run_query(sql)
        written[table] = len(table_rows)
    return written


# --- 5. verify -------------------------------------------------------------
def verify_identity_agreement(
    ch: ClickHouseMCP, production_id: str, revision_id: str, rows: dict[str, list[dict]]
) -> None:
    """Fail if the database's MATERIALIZED fact_key disagrees with the loader's.

    identity.py and the `facts` MATERIALIZED expression are two implementations of
    one rule. This is the check that stops them silently drifting apart.
    """
    scope = f"production_id = {_q(production_id)} AND revision_id = {_q(revision_id)}"
    db_keys = {
        r["fact_key"]
        for r in ch.rows(f"SELECT fact_key FROM facts FINAL WHERE {scope}")
    }
    ours = {
        fact_key(
            f["production_id"], f["kind"], f["established_in_scene_number"],
            f["entity_ids"], f["collision_ord"],
        )
        for f in rows["facts"]
    }
    if db_keys != ours:
        only_db = sorted(db_keys - ours)[:5]
        only_ours = sorted(ours - db_keys)[:5]
        raise IngestError(
            "fact_key mismatch between identity.py and the facts MATERIALIZED "
            f"column.\n  only in DB:     {only_db}\n  only in loader: {only_ours}\n"
            "Fix both together — they encode the same rule."
        )

    # every knowledge_state / dependency must point at a fact that exists
    for table, column in (("knowledge_state", "fact_key"), ("dependencies", "from_fact_key")):
        orphans = ch.rows(
            f"SELECT DISTINCT {column} AS k FROM {table} FINAL WHERE {scope} "
            f"AND {column} NOT IN (SELECT fact_key FROM facts FINAL WHERE {scope})"
        )
        if orphans:
            raise IngestError(
                f"{table} has {len(orphans)} entries pointing at facts that do not "
                f"exist in this revision: {[o['k'] for o in orphans[:5]]}"
            )


# --- orchestration ---------------------------------------------------------
def ingest_graph(
    graph: dict[str, Any], ch: ClickHouseMCP | None = None
) -> dict[str, Any]:
    """Validate, resolve, write and verify one revision's graph.

    Idempotent for the same revision_id: the sorting key of every graph table is
    the identity, so re-ingesting an identical graph replaces rows rather than
    adding them. See the caveat in the module docstring of __main__.
    """
    validate_graph(graph)
    rows = resolve(graph)

    owned = ch is None
    ch = ch or ClickHouseMCP().connect()
    try:
        _preserve_first_seen(ch, graph["production_id"], rows["entities"])
        written = write(ch, rows)
        verify_identity_agreement(
            ch, graph["production_id"], graph["revision_id"], rows
        )
    finally:
        if owned:
            ch.close()

    return {
        "production_id": graph["production_id"],
        "revision_id": graph["revision_id"],
        "written": written,
        "verified": True,
    }
