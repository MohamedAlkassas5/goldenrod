"""The screenplay fixtures and the hand-written answer key.

These tests are the guard on the evaluation set. The answer key is written by
hand, before the Gate exists — if a later edit makes it disagree with the script,
the seeds or the contracts, the whole precision number becomes meaningless. So
every citation in the key is checked against the actual files here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from services.loader.seed import seed_uuid, validate_seed

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "data" / "fixtures"
CONTRACTS = ROOT / "contracts"

PRODUCTION = "fayoum"
CURRENT_REVISION = "goldenrod-2026-08-29"
PRIOR_REVISION = "white-2026-08-01"

# Ranking order from db/clickhouse/schema.sql. Mirrored here only to assert the
# key is self-consistent; the live ranking uses the commitmentRank UDF.
RANK = {"shot": 0, "built": 1, "cast": 1, "permitted": 2,
        "scouted": 2, "sourced": 3, "planned": 4, "none": 5}


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _schema(name: str) -> Draft202012Validator:
    return Draft202012Validator(json.loads((CONTRACTS / name).read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def script_v1() -> list[str]:
    return (FIXTURES / "script-v1.fountain").read_text(encoding="utf-8").splitlines()


@pytest.fixture(scope="module")
def script_v2() -> list[str]:
    return (FIXTURES / "script-v2.fountain").read_text(encoding="utf-8").splitlines()


@pytest.fixture(scope="module")
def call_sheet() -> dict:
    return _load("call-sheet.json")


@pytest.fixture(scope="module")
def answer_key() -> dict:
    return _load("answer-key.json")


@pytest.fixture(scope="module")
def decisions() -> dict:
    return _load("decisions.seed.json")


@pytest.fixture(scope="module")
def commitments() -> dict:
    return _load("commitments.seed.json")


# --- the scripts -----------------------------------------------------------
def test_both_revisions_exist_and_are_locked(script_v1, script_v2):
    """Locked scene numbers are what makes fact identity stable across revisions."""
    n1 = re.findall(r"#(\d+[A-Z]?)#", "\n".join(script_v1))
    n2 = re.findall(r"#(\d+[A-Z]?)#", "\n".join(script_v2))
    assert n1 == n2, "scene numbers must not move between revisions on a locked script"
    assert len(n1) == 14
    assert len(set(n1)) == len(n1), "duplicate scene numbers"


def test_revision_changes_exactly_two_scenes(script_v1, script_v2):
    """The whole demo rests on the revision being small and the damage being
    somewhere else. If this ever grows, the planted break stops being clean."""
    import difflib

    changed_lines = [
        line for line in difflib.unified_diff(script_v1, script_v2, n=0)
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]
    body = [line for line in changed_lines if "Draft date" not in line and "Notes:" not in line]
    joined = "\n".join(body)

    # scene 18: the read is removed
    assert "She opens the letter and reads it." in joined
    assert "She turns the letter over. She does not open it." in joined
    # scene 31: the read arrives, and the letter is redated
    assert "dated MARCH 2019" in joined
    assert "dated MARCH 2022" in joined


def test_the_broken_scenes_are_byte_identical_between_revisions(script_v1, script_v2):
    """This is the point of the whole product: a text diff points at 18 and 31
    and says nothing at all about 22, 24 or 33."""
    for scene in ("22", "24", "33"):
        assert _scene_text(script_v1, scene) == _scene_text(script_v2, scene), (
            f"scene {scene} must be untouched — it is broken by consequence, not by edit"
        )


def _scene_text(lines: list[str], number: str) -> str:
    start = next(i for i, l in enumerate(lines) if l.rstrip().endswith(f"#{number}#"))
    end = next(
        (i for i in range(start + 1, len(lines))
         if re.match(r"^(INT|EXT)[\. ]", lines[i]) and re.search(r"#\d+[A-Z]?#", lines[i])),
        len(lines),
    )
    return "\n".join(lines[start:end]).strip()


# --- contracts -------------------------------------------------------------
def test_call_sheet_matches_its_contract(call_sheet):
    assert not list(_schema("call-sheet.schema.json").iter_errors(call_sheet))


def test_seeds_match_the_seed_contract(decisions, commitments):
    for doc in (decisions, commitments):
        assert not list(_schema("seed.schema.json").iter_errors(doc))
        validate_seed(doc)


def test_every_expected_finding_matches_the_finding_contract(answer_key):
    validator = _schema("finding.schema.json")
    for finding in answer_key["expected_findings"]:
        errors = list(validator.iter_errors(finding))
        assert not errors, f"{finding['finding_id']}: {[e.message for e in errors]}"


def test_no_finding_without_evidence(answer_key):
    """CLAUDE.md rule 1. The key must obey the rule it is measuring."""
    for finding in answer_key["expected_findings"]:
        assert len(finding["evidence"]) >= 1


# --- the key's citations must be real --------------------------------------
def test_every_quoted_line_exists_at_the_cited_line_number(answer_key, script_v2):
    for finding in answer_key["expected_findings"]:
        for ev in finding["evidence"]:
            if ev["type"] != "scene_line":
                continue
            actual = script_v2[ev["line"] - 1].strip()
            assert ev["quote"].strip() in actual or actual in ev["quote"].strip(), (
                f"{finding['finding_id']} cites line {ev['line']} as {ev['quote']!r} "
                f"but the script has {actual!r}"
            )


def test_cited_decisions_exist_are_active_and_reasons_match(answer_key, decisions):
    by_id = {
        seed_uuid(PRODUCTION, "decision", d["seed_key"]): d for d in decisions["decisions"]
    }
    cited = 0
    for finding in answer_key["expected_findings"]:
        for ev in finding["evidence"]:
            if ev["type"] != "decision":
                continue
            cited += 1
            seeded = by_id.get(ev["decision_id"])
            assert seeded, f"{finding['finding_id']} cites an unseeded decision"
            assert seeded["status"] == "active", "cannot cite a superseded decision"
            assert ev["reason"] == seeded["reason"], "reason drifted from the ledger"
    assert cited >= 1, "at least one finding must cite a logged decision"


def test_cited_commitments_match_the_seed(answer_key, commitments):
    by_key = {(c["entity_id"], c["scene_id"]): c for c in commitments["commitments"]}
    for finding in answer_key["expected_findings"]:
        for ev in finding["evidence"]:
            if ev["type"] != "commitment":
                continue
            seeded = by_key.get((ev["entity_id"], finding["scene"]))
            assert seeded, f"{finding['finding_id']} cites an unseeded commitment"
            assert seeded["state"] == ev["state"]


def test_each_finding_takes_the_highest_commitment_in_its_scene(answer_key, commitments):
    """Findings are ranked by what has already been paid for, so a finding's
    commitment_state must be the most-committed element in that scene."""
    for finding in answer_key["expected_findings"]:
        states = [
            c["state"] for c in commitments["commitments"] if c["scene_id"] == finding["scene"]
        ]
        highest = min(states, key=lambda s: RANK[s]) if states else "none"
        assert RANK[finding["commitment_state"]] == RANK[highest]


# --- the shape the demo needs ----------------------------------------------
def test_answer_key_ranking_spread_is_visible(answer_key):
    """SPEC 7: 'ranked by commitment cost, not by count'. The key must have a
    spread, or the ranking demonstrates nothing."""
    ranks = [RANK[f["commitment_state"]] for f in answer_key["expected_findings"]]
    assert ranks == sorted(ranks), "key should be written in ranked order"
    assert ranks[0] == 0, "top finding must be already shot"
    assert len(set(ranks)) >= 3, "need a visible spread across commitment states"


def test_call_sheet_has_one_broken_scene_and_two_clean_ones(call_sheet, answer_key):
    """data/fixtures/README.md requires exactly this shape."""
    scheduled = {s["scene_number"] for s in call_sheet["scenes"]}
    broken = {f["scene"] for f in answer_key["expected_findings"]}
    silent = {s["scene"] for s in answer_key["expected_silence"]}

    assert scheduled & broken, "no scheduled scene is broken; nothing to demo"
    assert len(scheduled & silent) >= 2, "need two scheduled scenes that are fine"
    assert not (broken & silent), "a scene cannot be both broken and expected-silent"


def test_the_changed_scene_itself_is_expected_silent(answer_key):
    """Scene 18 is what the revision CHANGED, not what it BROKE. Flagging it is
    crying wolf about the writer's own edit — precision over recall."""
    silent = {s["scene"] for s in answer_key["expected_silence"]}
    assert "18" in silent


def test_the_planted_break_is_second_order(answer_key):
    """Not a renamed location: the change lands in 18/31, the damage is in 22/24,
    and those scenes were never touched."""
    broken = {f["scene"] for f in answer_key["expected_findings"]}
    assert "22" in broken and "24" in broken
    assert "18" not in broken and "31" not in broken


def test_seeds_cover_every_scene_the_key_cites(answer_key, commitments):
    scenes = {c["scene_id"] for c in commitments["commitments"]}
    for finding in answer_key["expected_findings"]:
        assert finding["scene"] in scenes


def test_commitment_states_vary_enough_to_sort(commitments):
    states = {c["state"] for c in commitments["commitments"]}
    assert "shot" in states
    assert len(states) >= 5
    bands = {c["cost_band"] for c in commitments["commitments"]}
    assert "high" in bands and bands & {"none", "low"}


def test_decisions_all_carry_a_real_reason(decisions):
    for d in decisions["decisions"]:
        assert len(d["reason"].strip()) > 20, f"{d['seed_key']} has a thin reason"
    assert any(d["status"] == "superseded" for d in decisions["decisions"])


def test_fixtures_use_their_own_production_id(call_sheet, decisions, commitments, answer_key):
    """Kept separate from the `demo` and `test_seed` productions."""
    for doc in (call_sheet, decisions, commitments, answer_key):
        assert doc["production_id"] == PRODUCTION
