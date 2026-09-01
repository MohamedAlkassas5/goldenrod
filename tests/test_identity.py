"""The fact identity rule. Pure functions, no database."""

from __future__ import annotations

import copy

import pytest

from services.common.sql import insert, lit
from services.loader.identity import (
    assign_collision_ords,
    fact_key,
    fact_match_key,
    normalise_entity_ids,
)
from services.loader.ingest import IngestError, resolve, validate_graph

PROD = "test_demo"


# --- entity_ids order must not change fact_key -----------------------------
def test_entity_id_order_does_not_change_fact_key():
    a = fact_key(PROD, "knowledge", "18", ["entity:rana", "entity:letter"])
    b = fact_key(PROD, "knowledge", "18", ["entity:letter", "entity:rana"])
    assert a == b
    assert a == f"{PROD}|knowledge|18|entity:letter,entity:rana#1"


def test_entity_id_order_does_not_change_match_key():
    a = fact_match_key(PROD, "knowledge", ["b", "a", "c"])
    b = fact_match_key(PROD, "knowledge", ["c", "b", "a"])
    assert a == b == f"{PROD}|knowledge|a,b,c"


def test_duplicate_entity_ids_are_normalised_away():
    """One revision mentioning an entity twice must not change identity."""
    assert normalise_entity_ids(["x", "x", "y"]) == ["x", "y"]
    assert fact_key(PROD, "world", "5", ["x", "x", "y"]) == fact_key(
        PROD, "world", "5", ["y", "x"]
    )


def test_scene_number_is_part_of_identity():
    """A fact in a different scene is a different fact (tier 1)..."""
    assert fact_key(PROD, "knowledge", "18", ["a"]) != fact_key(
        PROD, "knowledge", "31", ["a"]
    )


def test_match_key_survives_a_scene_move():
    """...but the match key does not, which is what makes RELOCATED detectable."""
    assert fact_match_key(PROD, "knowledge", ["a"]) == fact_match_key(
        PROD, "knowledge", ["a"]
    )


# --- collision_ord must be deterministic -----------------------------------
def _collider(source_line: int, statement: str, scene: str = "18") -> dict:
    return {
        "production_id": PROD,
        "kind": "possession",
        "established_in_scene_number": scene,
        "entity_ids": ["entity:rana", "entity:letter"],
        "source_line": source_line,
        "statement": statement,
    }


def test_collision_ord_orders_by_source_line():
    facts = [_collider(410, "burns it"), _collider(300, "takes it")]
    assert assign_collision_ords(facts) == [2, 1]


def test_collision_ord_is_stable_under_input_reordering():
    facts = [_collider(300, "takes it"), _collider(410, "burns it"), _collider(360, "hides it")]
    keyed = {f["statement"]: o for f, o in zip(facts, assign_collision_ords(facts))}

    shuffled = [facts[2], facts[0], facts[1]]
    keyed_shuffled = {
        f["statement"]: o for f, o in zip(shuffled, assign_collision_ords(shuffled))
    }
    assert keyed == keyed_shuffled == {"takes it": 1, "hides it": 2, "burns it": 3}


def test_collision_ord_breaks_source_line_ties_deterministically():
    """Two facts on the same line must still get a total, repeatable order."""
    facts = [_collider(300, "zebra"), _collider(300, "aardvark")]
    assert assign_collision_ords(facts) == [2, 1]
    for _ in range(5):
        assert assign_collision_ords(copy.deepcopy(facts)) == [2, 1]


def test_non_colliding_facts_all_get_ord_one():
    facts = [_collider(300, "a", scene="18"), _collider(300, "a", scene="24")]
    assert assign_collision_ords(facts) == [1, 1]


# --- passthrough ids are never identity ------------------------------------
def test_fact_id_is_not_part_of_identity(graph_v1):
    """Renaming every passthrough handle must not change any fact_key."""
    before = {f["fact_key"] for f in _keys(graph_v1)}

    renamed = copy.deepcopy(graph_v1)
    renamed["facts"][0]["fact_id"] = "COMPLETELY-DIFFERENT"
    renamed["knowledge_state"][0]["fact_id"] = "COMPLETELY-DIFFERENT"
    renamed["dependencies"][0]["from_fact_id"] = "COMPLETELY-DIFFERENT"
    renamed["dependencies"][0]["dependency_id"] = "also-different"

    assert {f["fact_key"] for f in _keys(renamed)} == before


def test_source_line_is_not_part_of_identity(graph_v1):
    """Pages re-flow on every revision; that must not move a fact's identity."""
    before = {f["fact_key"] for f in _keys(graph_v1)}
    shifted = copy.deepcopy(graph_v1)
    shifted["facts"][0]["source_line"] = 9999
    assert {f["fact_key"] for f in _keys(shifted)} == before


def test_statement_is_not_part_of_identity(graph_v1):
    """A changed statement is the diff signal, not a new fact."""
    before = {f["fact_key"] for f in _keys(graph_v1)}
    edited = copy.deepcopy(graph_v1)
    edited["facts"][0]["statement"] = "Rana has read the letter, twice"
    assert {f["fact_key"] for f in _keys(edited)} == before


def _keys(graph):
    rows = resolve(graph)["facts"]
    for r in rows:
        r["fact_key"] = fact_key(
            r["production_id"], r["kind"], r["established_in_scene_number"],
            r["entity_ids"], r["collision_ord"],
        )
    return rows


# --- the planted 18 -> 31 relocation ---------------------------------------
def test_planted_relocation_shape(graph_v1, graph_v2):
    """v1 -> v2 must read as RELOCATED, not as an unrelated remove + add."""
    f1 = _keys(graph_v1)[0]
    f2 = _keys(graph_v2)[0]

    # tier 1: the full keys differ, because the scene is part of identity
    assert f1["fact_key"] != f2["fact_key"]
    assert f1["established_in_scene_number"] == "18"
    assert f2["established_in_scene_number"] == "31"

    # tier 2: the match keys are equal, so the diff reports one RELOCATED fact
    m1 = fact_match_key(f1["production_id"], f1["kind"], f1["entity_ids"])
    m2 = fact_match_key(f2["production_id"], f2["kind"], f2["entity_ids"])
    assert m1 == m2

    # and the knowledge break is carried on knowledge_state, not on the text
    assert resolve(graph_v1)["knowledge_state"][0]["knows"] == 1
    assert resolve(graph_v2)["knowledge_state"][0]["knows"] == 0


def test_relocation_preserves_dependency_to_scene_24(graph_v1, graph_v2):
    """The second-order hop must survive the move: 24 still depends on the fact."""
    for graph in (graph_v1, graph_v2):
        dep = resolve(graph)["dependencies"][0]
        assert dep["to_scene_number"] == "24"
        assert dep["from_fact_key"] == _keys(graph)[0]["fact_key"]


# --- validation ------------------------------------------------------------
def test_invalid_enum_is_rejected_before_any_write(graph_v1):
    graph_v1["facts"][0]["kind"] = "vibes"
    with pytest.raises(IngestError) as exc:
        validate_graph(graph_v1)
    assert "vibes" in str(exc.value)


def test_invalid_entity_type_is_rejected(graph_v1):
    graph_v1["entities"][0]["type"] = "macguffin"
    with pytest.raises(IngestError):
        validate_graph(graph_v1)


def test_invalid_day_night_is_rejected(graph_v1):
    graph_v1["scenes"][0]["day_night"] = "MIDNIGHT"
    with pytest.raises(IngestError):
        validate_graph(graph_v1)


def test_knowledge_state_and_dependencies_are_required(graph_v1):
    """They are the product (SPEC 4.1); the contract now requires them."""
    del graph_v1["knowledge_state"]
    with pytest.raises(IngestError) as exc:
        validate_graph(graph_v1)
    assert "knowledge_state" in str(exc.value)


def test_dangling_scene_reference_is_rejected(graph_v1):
    """ClickHouse has no foreign keys, so the loader enforces this."""
    graph_v1["facts"][0]["established_in_scene_id"] = "s99"
    validate_graph(graph_v1)  # schema-valid...
    with pytest.raises(IngestError) as exc:  # ...but referentially broken
        resolve(graph_v1)
    assert "s99" in str(exc.value)


def test_dangling_fact_reference_is_rejected(graph_v1):
    graph_v1["dependencies"][0]["from_fact_id"] = "nope"
    with pytest.raises(IngestError) as exc:
        resolve(graph_v1)
    assert "nope" in str(exc.value)


# --- SQL rendering ---------------------------------------------------------
def test_lit_escapes_quotes_and_backslashes():
    assert lit("O'Hara") == r"'O\'Hara'"
    assert lit("back\\slash") == r"'back\\slash'"
    assert lit("line\nbreak") == r"'line\nbreak'"


def test_lit_renders_arrays_and_scalars():
    assert lit(["b", "a"]) == "['b', 'a']"
    assert lit([]) == "[]"
    assert lit(7) == "7"
    assert lit(True) == "1"
    assert lit(None) == "''"


def test_insert_rejects_unsafe_identifiers():
    with pytest.raises(ValueError):
        insert("facts; DROP TABLE x", ["a"], [{"a": 1}])


def test_insert_is_empty_for_no_rows():
    assert insert("facts", ["a"], []) == ""


# --- schema statement splitting --------------------------------------------
def test_split_statements_does_not_drop_ddl_behind_comment_blocks():
    """A naive text.split(';') silently skips statements preceded by a comment
    block. That bug would leave tables uncreated with no error."""
    from services.common.sql import split_statements

    sql = """
-- ------------------------------------
-- a comment block before the statement
-- ------------------------------------
CREATE TABLE IF NOT EXISTS a (x String) ENGINE = MergeTree ORDER BY x;

-- another comment
CREATE TABLE IF NOT EXISTS b (y String) ENGINE = MergeTree ORDER BY y;
"""
    stmts = split_statements(sql)
    assert len(stmts) == 2
    assert all(s.startswith("CREATE TABLE") for s in stmts)


def test_split_statements_handles_the_real_schema():
    from pathlib import Path

    from services.common.sql import split_statements

    root = Path(__file__).resolve().parents[1]
    stmts = split_statements((root / "db" / "clickhouse" / "schema.sql").read_text(encoding="utf-8"))
    tables = [s.split()[5] for s in stmts if s.upper().startswith("CREATE TABLE")]
    assert tables == [
        "scenes", "entities", "facts", "knowledge_state", "dependencies",
        "decisions", "commitments", "findings", "access_grants", "access_log",
    ]
    assert any("FUNCTION" in s.upper() for s in stmts)
