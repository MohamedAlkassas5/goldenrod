"""Seed loader: validation, determinism, and the commitment ranking.

Seeds into its own production_id so assertions are unaffected by whatever else
is in the demo production.
"""

from __future__ import annotations

import copy

import pytest

from services.common.mcp_client import ClickHouseMCP, MCPError
from services.common.queries import bind, get_query, load_queries
from services.loader.seed import (
    SeedError,
    load_seed_files,
    resolve_seed,
    seed_ledger,
    seed_uuid,
    validate_seed,
)

TEST_PRODUCTION = "test_seed"

# What the ranking must produce, in order. This is the product: findings are
# ordered by what has already been paid for, not by count or severity.
EXPECTED_RANKING = [
    ("24", "entity:rana", "shot", 0),      # already shot - a re-shoot is a company day
    ("24", "entity:letter", "built", 1),   # hero prop made
    ("24", "entity:flat", "scouted", 2),   # location agreement signed
    ("31", "entity:letter", "sourced", 3), # stock ordered
    ("31", "entity:rana", "planned", 4),   # on the one-liner only
    ("18", "entity:rana", "", 5),          # LEFT JOIN miss -> ranks as 'none'
    ("12", "entity:street", "none", 5),    # nothing committed
]


@pytest.fixture(scope="module")
def seed_doc():
    doc = copy.deepcopy(load_seed_files())
    doc["production_id"] = TEST_PRODUCTION
    return doc


# --- valid seed data -------------------------------------------------------
def test_shipped_seed_is_valid():
    validate_seed(load_seed_files())


def test_shipped_seed_meets_the_demo_requirements():
    """data/fixtures/README.md: at least one decision must connect to the planted
    break, and commitment state must vary enough for ranking to be meaningful."""
    doc = load_seed_files()
    decisions = doc["decisions"]
    commitments = doc["commitments"]

    planted = [d for d in decisions if d["seed_key"] == "withhold-letter-reveal"]
    assert len(planted) == 1
    assert {"entity:letter", "entity:rana"} <= set(planted[0]["entity_ids"])
    assert planted[0]["reason"].strip()

    assert all(d["reason"].strip() for d in decisions), "reason is mandatory"
    assert {d["cause_tag"] for d in decisions} <= {
        "taste", "constraint", "experiment", "external_note"
    }

    states = {c["state"] for c in commitments}
    assert len(states) >= 4, "ranking needs something to sort"
    assert "shot" in states, "need a high-commitment row for the top of the ranking"
    assert {c["cost_band"] for c in commitments} & {"high"}
    assert {c["cost_band"] for c in commitments} & {"none", "low"}

    assert any(d["status"] == "superseded" for d in decisions), (
        "need a superseded decision to prove the query filters on status"
    )


# --- determinism -----------------------------------------------------------
def test_seed_uuid_is_stable():
    a = seed_uuid("demo", "decision", "withhold-letter-reveal")
    b = seed_uuid("demo", "decision", "withhold-letter-reveal")
    assert a == b
    assert a != seed_uuid("demo", "commitment", "withhold-letter-reveal")
    assert a != seed_uuid("other", "decision", "withhold-letter-reveal")


def test_resolve_is_deterministic():
    doc = load_seed_files()
    assert resolve_seed(doc) == resolve_seed(copy.deepcopy(doc))


def test_entity_ids_are_normalised():
    doc = {
        "production_id": "p",
        "decisions": [{
            "seed_key": "k", "scene_id": "1", "entity_ids": ["b", "a", "b"],
            "decision_type": "story", "selected_option": "x", "reason": "y",
            "cause_tag": "taste", "decided_by": "z", "decided_at": "2026-01-01 00:00:00",
        }],
    }
    assert resolve_seed(doc)["decisions"][0]["entity_ids"] == ["a", "b"]


# --- invalid enum / state rejection ----------------------------------------
@pytest.mark.parametrize(
    "path, value",
    [
        (("decisions", 0, "decision_type"), "vibes"),
        (("decisions", 0, "cause_tag"), "hunch"),
        (("decisions", 0, "status"), "maybe"),
        (("decisions", 0, "reason"), ""),
        (("decisions", 0, "selected_option"), ""),
        (("commitments", 0, "state"), "committed"),
        (("commitments", 0, "cost_band"), "enormous"),
        (("commitments", 0, "entity_id"), ""),
    ],
)
def test_invalid_values_are_rejected(path, value):
    doc = copy.deepcopy(load_seed_files())
    table, index, field = path
    doc[table][index][field] = value
    with pytest.raises(SeedError):
        validate_seed(doc)


def test_missing_reason_is_rejected():
    doc = copy.deepcopy(load_seed_files())
    del doc["decisions"][0]["reason"]
    with pytest.raises(SeedError) as exc:
        validate_seed(doc)
    assert "reason" in str(exc.value)


def test_unknown_field_is_rejected():
    """additionalProperties: false, so a typo cannot silently vanish."""
    doc = copy.deepcopy(load_seed_files())
    doc["decisions"][0]["casue_tag"] = "taste"
    with pytest.raises(SeedError):
        validate_seed(doc)


def test_duplicate_seed_key_is_rejected():
    doc = copy.deepcopy(load_seed_files())
    doc["decisions"].append(copy.deepcopy(doc["decisions"][0]))
    with pytest.raises(SeedError) as exc:
        validate_seed(doc)
    assert "duplicate seed_key" in str(exc.value)


# --- one ranking implementation --------------------------------------------
def test_ranking_query_comes_from_schema_sql():
    queries = load_queries()
    assert "commitment_ranking" in queries
    sql = queries["commitment_ranking"]
    assert "commitmentRank(" in sql, "ranking must use the UDF, not inline logic"
    assert "argMax" in sql and "GROUP BY" in sql, "must be real analytical work"


def test_no_second_ranking_implementation_in_python():
    """CLAUDE.md rule 3 / requirement: commitmentRank is the only ranking."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "services"
    offenders = [
        str(p.relative_to(root.parent))
        for p in root.rglob("*.py")
        if re.search(r"['\"]shot['\"]\s*:\s*0|shot.*=.*0\b.*built.*=.*1", p.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"rank mapping re-implemented in {offenders}"


# --- integration -----------------------------------------------------------
@pytest.fixture(scope="module")
def ch():
    try:
        client = ClickHouseMCP().connect()
        client.run_query("SELECT 1")
    except (MCPError, OSError, FileNotFoundError) as exc:
        pytest.skip(f"no ClickHouse reachable through MCP: {exc}")
    yield client
    client.close()


@pytest.mark.mcp
class TestAgainstClickHouse:
    def test_seeding_twice_inserts_decisions_once(self, ch, seed_doc):
        first = seed_ledger(seed_doc, ch)
        second = seed_ledger(seed_doc, ch)

        assert first["decisions_inserted"] + first["decisions_skipped"] == 7
        assert second["decisions_inserted"] == 0, "append-only table double-seeded"
        assert second["decisions_skipped"] == 7

    def test_repeated_seeding_leaves_row_counts_stable(self, ch, seed_doc):
        seed_ledger(seed_doc, ch)
        before = self._counts(ch)
        seed_ledger(seed_doc, ch)
        assert self._counts(ch) == before

    @staticmethod
    def _counts(ch):
        scope = f"production_id = '{TEST_PRODUCTION}'"
        return (
            ch.scalar(f"SELECT count() FROM decisions WHERE {scope}"),
            ch.scalar(f"SELECT count() FROM commitments FINAL WHERE {scope}"),
        )

    def test_commitment_states_all_landed(self, ch, seed_doc):
        seed_ledger(seed_doc, ch)
        rows = ch.rows(
            f"SELECT state, cost_band FROM commitments FINAL "
            f"WHERE production_id = '{TEST_PRODUCTION}'"
        )
        assert {r["state"] for r in rows} == {
            "shot", "built", "scouted", "sourced", "planned", "none"
        }
        assert "high" in {r["cost_band"] for r in rows}

    def test_ranking_query_returns_expected_order(self, ch, seed_doc):
        """The reference query from schema.sql, run for real. This ordering is
        the product: what has already been paid for comes first."""
        seed_ledger(seed_doc, ch)

        entities = sorted({c["entity_id"] for c in seed_doc["commitments"]} |
                          {e for d in seed_doc["decisions"] for e in d["entity_ids"]})
        sql = bind(
            get_query("commitment_ranking"),
            production=TEST_PRODUCTION,
            affected_entities=entities,
        )
        rows = ch.rows(sql)

        actual = [
            (r["scene_id"], r["entity_id"], r["commitment_state"], r["commitment_rank"])
            for r in rows
        ]
        assert actual == EXPECTED_RANKING

    def test_ranking_excludes_superseded_decisions(self, ch, seed_doc):
        seed_ledger(seed_doc, ch)
        sql = bind(
            get_query("commitment_ranking"),
            production=TEST_PRODUCTION,
            affected_entities=["entity:letter", "entity:rana"],
        )
        rows = ch.rows(sql)
        choices = {r["current_choice"] for r in rows}
        assert "Reveal the letter in 18" not in choices, "superseded decision leaked"

    def test_planted_break_decision_carries_its_reason(self, ch, seed_doc):
        """A finding must be able to cite WHY, not just a scene and a line."""
        seed_ledger(seed_doc, ch)
        sql = bind(
            get_query("commitment_ranking"),
            production=TEST_PRODUCTION,
            affected_entities=["entity:letter", "entity:rana"],
        )
        top = ch.rows(sql)[0]
        assert top["commitment_state"] == "shot"
        assert top["scene_id"] == "24"
        assert "network note" in top["current_reason"].lower()

    def test_aggregation_counts_decisions_per_entity(self, ch, seed_doc):
        """Real aggregation, not a lookup (CLAUDE.md rule 5)."""
        seed_ledger(seed_doc, ch)
        sql = bind(
            get_query("commitment_ranking"),
            production=TEST_PRODUCTION,
            affected_entities=["entity:rana"],
        )
        rows = ch.rows(sql)
        assert all(r["decisions_touching"] >= 1 for r in rows)
        assert any(r["taste_decisions"] >= 1 for r in rows)
        assert all(isinstance(r["decision_types"], list) for r in rows)
