"""The Gate: detection, evidence, ranking, and the run against the answer key.

The unit half runs anywhere — the rules, the ordering and the evidence
enforcement are pure functions over rows, on purpose, so precision can be tuned
without a database or a model.

The integration half drives the whole pipeline over the real fixture
screenplays, through the MCP server, and scores the result against
`data/fixtures/answer-key.json`. That score is the acceptance criterion in
SPEC §8, and this is where it is computed.

The Extractor's model pass is stood in for by a scripted backend, exactly as in
tests/test_extractor.py. Everything else — the parse, the identity, the ingest,
the four ClickHouse queries, the traversal, the ranking — is the real thing.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from services.gate.evidence import (
    ScriptLines,
    best_decision,
    carry_forward,
    commitment,
    decision,
    dedupe,
    scene_line,
)
from services.gate.findings import build_findings, dismissal_id, finding_id, severity_for
from services.gate.rules import RULE_A, RULE_B, RULE_C, ChangedFact, DependencyEdge, detect
from services.gate.run import GateError, load_call_sheet, validate_call_sheet
from services.gate.scene_order import SceneOrder, scene_sort_key
from tests.demo_graph import load_production

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "data" / "fixtures"

PRODUCTION = "test_gate"
PRIOR_REVISION = "white-2026-08-01"
CURRENT_REVISION = "goldenrod-2026-08-29"

READ_KEY = f"{PRODUCTION}|knowledge|entity:letter,entity:rana"
DATE_KEY = f"{PRODUCTION}|temporal|entity:letter"


# ===========================================================================
# Script order — the ordering every claim about "before" and "after" rests on
# ===========================================================================
def test_locked_scene_numbers_sort_in_script_order():
    order = SceneOrder.of(["31", "8", "24A", "12", "A24", "24", "2"])
    assert order.numbers == ("2", "8", "12", "A24", "24", "24A", "31")


def test_a_scene_number_that_cannot_be_placed_is_not_guessed_at():
    order = SceneOrder.of(["18", "24"])
    assert order.knows("18") is True
    assert order.knows("PROLOGUE") is False
    assert order.precedes("PROLOGUE", "24") is None
    assert order.precedes("18", "24") is True
    assert order.precedes("24", "18") is False


def test_unplaceable_numbers_sort_last_without_crashing():
    assert SceneOrder.of(["24", "OMITTED", "8"]).numbers == ("8", "24", "OMITTED")
    assert scene_sort_key("24A")[:2] == (24, 1)
    assert scene_sort_key("A24")[:2] == (24, -1)


# ===========================================================================
# The rules — pure, over synthetic rows
# ===========================================================================
def _fact(change: str, was: str = "", now: str = "", was_st: str = "", now_st: str = ""):
    return ChangedFact(
        match_key=READ_KEY,
        change=change,
        was_scene=was,
        now_scene=now,
        was_statement=was_st or "Rana has read the letter",
        now_statement=now_st or "Rana has read the letter",
    )


def _edge(scene: str, line: int = 100, kind: str = "references", fact_kind="knowledge"):
    return DependencyEdge(
        scene=scene,
        fact_key=f"{PRODUCTION}|{fact_kind}|31|entity:letter,entity:rana#1",
        fact_match_key=READ_KEY,
        fact_kind=fact_kind,
        statement="Rana has read the letter",
        established_in_scene="31",
        source_line=180,
        entity_ids=("entity:letter", "entity:rana"),
        dependency_kind=kind,
        evidence_line=line,
        evidence_quote="You knew she sold Fayoum.",
    )


ORDER = SceneOrder.of(["18", "22", "24", "26", "27", "31", "33", "36"])


def test_a_scene_before_the_fact_it_depends_on_is_a_break():
    breaks = detect([_fact("RELOCATED", "18", "31")], [_edge("24")], ORDER)
    assert [(b.scene, b.rule, b.kind) for b in breaks] == [
        ("24", RULE_A, "knowledge_state")
    ]
    assert "scene 18 to scene 31" in breaks[0].claim


def test_a_scene_after_the_fact_is_not_a_break():
    assert detect([_fact("RELOCATED", "18", "31")], [_edge("36")], ORDER) == []


def test_the_scene_the_revision_changed_is_never_flagged():
    """Precision over recall: that is the writer's own edit, correct as written."""
    assert detect([_fact("RELOCATED", "18", "31")], [_edge("31")], ORDER) == []


def test_an_unchanged_fact_produces_nothing_even_when_the_order_looks_wrong():
    """A pre-existing property of the script is not something this week broke."""
    other = ChangedFact(match_key="something:else", change="RELOCATED", now_scene="31")
    assert detect([other], [_edge("24")], ORDER) == []


def test_a_restated_fact_breaks_the_scenes_after_it():
    fact = _fact("RELOCATED", "18", "31", "dated March 2019", "dated March 2022")
    breaks = detect([fact], [_edge("33", 207, "assumes", "temporal")], ORDER)
    assert [(b.scene, b.rule, b.kind) for b in breaks] == [("33", RULE_B, "temporal")]
    assert "March 2019" in breaks[0].claim and "March 2022" in breaks[0].claim


def test_a_restated_fact_does_not_double_flag_a_scene_before_it():
    """One scene, one rule: the ordering break is the stronger claim."""
    fact = _fact("RELOCATED", "18", "31", "dated March 2019", "dated March 2022")
    breaks = detect([fact], [_edge("24"), _edge("33", 207)], ORDER)
    assert sorted((b.scene, b.rule) for b in breaks) == [
        ("24", RULE_A), ("33", RULE_B)
    ]


def test_a_removed_fact_leaves_a_dangling_reference():
    fact = ChangedFact(match_key=READ_KEY, change="REMOVED", was_scene="18")
    breaks = detect(
        [fact], [_edge("24")], ORDER, current_scenes=frozenset({"18", "24"})
    )
    assert [(b.scene, b.rule, b.kind) for b in breaks] == [
        ("24", RULE_C, "reference_break")
    ]


def test_a_removed_fact_in_a_scene_that_is_also_gone_is_not_flagged():
    fact = ChangedFact(match_key=READ_KEY, change="REMOVED", was_scene="18")
    assert detect([fact], [_edge("24")], ORDER, current_scenes=frozenset({"18"})) == []


def test_no_claim_is_made_when_the_ordering_is_unknown():
    order = SceneOrder.of(["24", "31"])
    assert detect([_fact("RELOCATED", "18", "31")], [_edge("PROLOGUE")], order) == []


def test_several_edges_in_one_scene_become_one_finding_with_both_citations():
    edges = [_edge("24", 121), _edge("24", 140)]
    breaks = detect([_fact("RELOCATED", "18", "31")], edges, ORDER)
    assert len(breaks) == 1
    assert [e.evidence_line for e in breaks[0].edges] == [121, 140]


# ===========================================================================
# Evidence — CLAUDE.md rule 1, enforced in code
# ===========================================================================
@pytest.fixture(scope="module")
def pages_v2() -> ScriptLines:
    return ScriptLines.from_path(FIXTURES / "script-v2.fountain")


def test_a_line_is_quoted_from_the_file_not_from_a_paraphrase(pages_v2):
    assert pages_v2.quote(184) == "Handwritten, dated MARCH 2022."
    assert pages_v2.quote(99999) is None


def test_a_quote_is_re_found_at_its_new_line_after_the_revision(pages_v2):
    """Scene 22 is byte-identical between revisions but sits eight lines higher."""
    assert pages_v2.find_unique("She sold it. Was it you who did the papers?") == 98
    assert pages_v2.find_unique("You knew she sold Fayoum.") == 121


def test_an_ambiguous_or_absent_quote_is_not_placed(pages_v2):
    assert pages_v2.find_unique("RANA") is None          # a cue on many pages
    assert pages_v2.find_unique("What?") is None         # too short to identify
    assert pages_v2.find_unique("She reads it and weeps.") is None  # not in the file


def test_evidence_without_a_quote_or_a_reason_is_not_built():
    assert scene_line("24", 121, "") is None
    assert scene_line("24", 0, "You knew she sold Fayoum.") is None
    assert decision({"decision_id": "d1", "reason": "   "}) is None
    assert decision({"reason": "because"}) is None
    assert commitment({"entity_id": "entity:rana"}) is None


def test_evidence_is_deduplicated_but_keeps_its_order():
    a = scene_line("24", 121, "You knew she sold Fayoum.")
    assert dedupe([a, None, dict(a), commitment({"entity_id": "e", "state": "shot"})]) == [
        a, {"type": "commitment", "entity_id": "e", "state": "shot"}
    ]


def test_the_decision_cited_is_the_one_about_this_fact_not_the_prop_stock():
    rows = [
        {"decision_id": "hold", "entity_ids": ["entity:letter", "entity:rana"],
         "decided_at": "2026-08-27 15:10:00", "reason": "hold the reveal"},
        {"decision_id": "redate", "entity_ids": ["entity:letter"],
         "decided_at": "2026-08-27 15:40:00", "reason": "redate it"},
    ]
    # More recent, but it only touches the letter: the fact is about both.
    assert best_decision(rows, ["entity:letter", "entity:rana"])["decision_id"] == "hold"
    # About the letter alone, the most recent letter decision wins.
    assert best_decision(rows, ["entity:letter"])["decision_id"] == "redate"
    assert best_decision(rows, ["entity:tarek"]) is None


def test_a_carried_citation_is_repointed_at_the_current_pages(pages_v2):
    stale = _edge("22", 106)
    stale = type(stale)(**{**stale.__dict__,
                           "evidence_quote": "She sold it. Was it you who did the papers?"})
    current = {READ_KEY: {"fact_key": "k", "kind": "knowledge", "statement": "s",
                          "established_in_scene": "31", "source_line": 180,
                          "fact_entity_ids": ["entity:letter", "entity:rana"]}}
    carried, notes = carry_forward([stale], current, pages_v2, PRIOR_REVISION)

    assert notes == []
    assert carried[0].evidence_line == 98, "line 106 in the white pages is 98 in goldenrod"
    assert carried[0].cited_revision == "", "it is a current-revision citation now"
    assert carried[0].established_in_scene == "31" and carried[0].source_line == 180


def test_a_citation_that_cannot_be_placed_is_labelled_with_its_own_revision(pages_v2):
    stale = _edge("22", 106)  # quote "You knew she sold Fayoum." is in 24, not 22
    current = {READ_KEY: {"fact_key": "k", "kind": "knowledge", "statement": "s",
                          "established_in_scene": "31", "source_line": 180,
                          "fact_entity_ids": []}}
    carried, notes = carry_forward([stale], current, None, PRIOR_REVISION)
    assert carried[0].evidence_line == 106
    assert carried[0].cited_revision == PRIOR_REVISION
    assert notes and PRIOR_REVISION in notes[0], "a weakened citation is reported"


# ===========================================================================
# Findings — the drop rule, severity, ranking, suppression
# ===========================================================================
def _commitments(**by_scene) -> list[dict]:
    rows = []
    for scene, entries in by_scene.items():
        for entity, state, rank, type_ in entries:
            rows.append(
                {"scene_id": scene.lstrip("s"), "entity_id": entity, "entity_name": entity,
                 "entity_type": type_, "commitment_state": state, "commitment_rank": rank,
                 "cost_band": "high", "committed_at": "2026-08-28 21:15:00"}
            )
    return rows


def _build(breaks, commitments=(), decisions=(), **kw):
    return build_findings(
        breaks,
        production_id=PRODUCTION,
        shoot_date="2026-09-04",
        revision_id=CURRENT_REVISION,
        commitments=list(commitments),
        decisions=list(decisions),
        order=ORDER,
        **kw,
    )


def test_severity_comes_from_the_rank_clickhouse_computed():
    assert [severity_for(r) for r in range(6)] == [
        "high", "high", "medium", "medium", "medium", "low"
    ]


def test_a_finding_with_no_evidence_is_dropped_not_rendered():
    """CLAUDE.md rule 1, in code rather than in a prompt."""
    naked = _edge("24", line=0)
    naked = type(naked)(**{**naked.__dict__, "evidence_quote": ""})
    findings, dropped, _ = _build(detect([_fact("RELOCATED", "18", "31")], [naked], ORDER))
    assert findings == []
    assert dropped and "no evidence" in dropped[0]


def test_every_finding_that_ships_matches_the_finding_contract():
    from jsonschema import Draft202012Validator

    schema = json.loads((ROOT / "contracts" / "finding.schema.json").read_text("utf-8"))
    findings, _, _ = _build(
        detect([_fact("RELOCATED", "18", "31")], [_edge("24")], ORDER),
        _commitments(s24=[("entity:rana", "cast", 1, "character")]),
    )
    assert findings
    for finding in findings:
        assert not list(Draft202012Validator(schema).iter_errors(finding))


def test_findings_are_ranked_by_commitment_state_not_by_script_order():
    breaks = detect([_fact("RELOCATED", "18", "31")], [_edge("22"), _edge("24")], ORDER)
    commitments = _commitments(
        s22=[("entity:rana", "planned", 4, "character")],
        s24=[("entity:rana", "shot", 0, "character")],
    )
    findings, _, _ = _build(breaks, commitments)
    assert [f["scene"] for f in findings] == ["24", "22"], "the shot scene comes first"
    assert [f["severity"] for f in findings] == ["high", "medium"]


def test_the_order_changes_when_a_commitment_state_changes():
    """SPEC §8: the order must visibly change when commitment state changes."""
    breaks = detect([_fact("RELOCATED", "18", "31")], [_edge("22"), _edge("24")], ORDER)
    before, _, _ = _build(
        breaks,
        _commitments(
            s22=[("entity:rana", "planned", 4, "character")],
            s24=[("entity:rana", "shot", 0, "character")],
        ),
    )
    after, _, _ = _build(
        breaks,
        _commitments(
            s22=[("entity:rana", "shot", 0, "character")],
            s24=[("entity:rana", "planned", 4, "character")],
        ),
    )
    assert [f["scene"] for f in before] == ["24", "22"]
    assert [f["scene"] for f in after] == ["22", "24"]


def test_a_finding_takes_the_most_committed_element_in_its_scene():
    breaks = detect([_fact("RELOCATED", "18", "31")], [_edge("24")], ORDER)
    findings, _, _ = _build(
        breaks,
        _commitments(
            s24=[
                ("entity:flat", "permitted", 2, "location"),
                ("entity:rana", "cast", 1, "character"),
                ("entity:letter", "built", 1, "prop"),
            ]
        ),
    )
    assert findings[0]["commitment_state"] == "cast"
    cited = [e for e in findings[0]["evidence"] if e["type"] == "commitment"]
    assert cited[0]["entity_id"] == "entity:rana", "a character, not the room"


def test_a_scene_with_nothing_committed_ranks_last_and_reads_low():
    breaks = detect([_fact("RELOCATED", "18", "31")], [_edge("24")], ORDER)
    findings, _, _ = _build(breaks)
    assert findings[0]["commitment_state"] == "none"
    assert findings[0]["severity"] == "low"


def test_an_already_shot_scene_is_told_to_raise_a_pickup_not_to_cut_a_line():
    breaks = detect([_fact("RELOCATED", "18", "31")], [_edge("22")], ORDER)
    findings, _, _ = _build(
        breaks, _commitments(s22=[("entity:rana", "shot", 0, "character")])
    )
    assert "pickup" in findings[0]["suggested_action"]


def test_finding_ids_are_stable_across_runs_but_not_across_scenes():
    first = finding_id(PRODUCTION, "24", RULE_A, READ_KEY)
    assert first == finding_id(PRODUCTION, "24", RULE_A, READ_KEY)
    assert first != finding_id(PRODUCTION, "22", RULE_A, READ_KEY)
    assert first != finding_id("other", "24", RULE_A, READ_KEY)


# --- the intentional-deviation loop ----------------------------------------
def _dismissal_of(finding: dict, scene: str, entity_ids: list[str]) -> dict:
    return {
        "decision_id": dismissal_id(PRODUCTION, finding["finding_id"]),
        "scene_id": scene,
        "entity_ids": entity_ids,
        "reason": "Rana confronts Tarek on suspicion, not on the letter.",
        "deviation_reason": "Rana confronts Tarek on suspicion, not on the letter.",
        "decided_by": "script.coordinator@fayoum",
        "decided_at": "2026-09-03 20:10:00",
        "intentional_deviation": 1,
    }


def test_marking_a_finding_intentional_silences_it_on_the_next_run():
    breaks = detect([_fact("RELOCATED", "18", "31")], [_edge("22"), _edge("24")], ORDER)
    commitments = _commitments(
        s22=[("entity:rana", "shot", 0, "character")],
        s24=[("entity:rana", "cast", 1, "character")],
    )
    before, _, _ = _build(breaks, commitments)
    assert [f["scene"] for f in before] == ["22", "24"]

    note = _dismissal_of(before[1], "24", ["entity:rana"])
    after, _, silenced = _build(breaks, commitments, [note])

    assert [f["scene"] for f in after] == ["22"], "the marked finding is gone"
    assert [f["scene"] for f in silenced] == ["24"]
    assert silenced[0]["dismissed"] == {
        "intentional": True,
        "reason": note["deviation_reason"],
        "marked_by": "script.coordinator@fayoum",
        "marked_at": "2026-09-03T20:10:00",
    }


def test_a_related_finding_reads_differently_after_the_deviation_is_accepted():
    """SPEC §7, 2:00–2:30: a related flag resolves differently, on screen."""
    breaks = detect([_fact("RELOCATED", "18", "31")], [_edge("22"), _edge("24")], ORDER)
    commitments = _commitments(
        s22=[("entity:rana", "shot", 0, "character")],
        s24=[("entity:rana", "cast", 1, "character")],
    )
    before, _, _ = _build(breaks, commitments)
    note = _dismissal_of(before[1], "24", ["entity:rana"])
    after, _, _ = _build(breaks, commitments, [note])

    was, now = before[0]["suggested_action"], after[0]["suggested_action"]
    assert was != now
    assert "scene 24" in now
    assert note["decision_id"] in [
        e.get("decision_id") for e in after[0]["evidence"] if e["type"] == "decision"
    ], "the accepted deviation is cited as evidence, not merely remembered"


def test_a_deviation_on_an_unrelated_element_changes_nothing():
    breaks = detect([_fact("RELOCATED", "18", "31")], [_edge("22")], ORDER)
    before, _, _ = _build(breaks)
    note = _dismissal_of(before[0], "40", ["entity:hamdy"])
    note["decision_id"] = "00000000-0000-0000-0000-000000000001"
    after, _, silenced = _build(breaks, decisions=[note])
    assert silenced == []
    assert after[0]["suggested_action"] == before[0]["suggested_action"]


# ===========================================================================
# The call sheet — the trigger
# ===========================================================================
def test_the_fixture_call_sheet_loads():
    call_sheet = load_call_sheet(FIXTURES / "call-sheet.json")
    assert [s["scene_number"] for s in call_sheet["scenes"]] == ["18", "24", "26", "27"]


def test_a_call_sheet_that_breaks_its_contract_is_refused():
    with pytest.raises(GateError, match="call-sheet.schema.json"):
        validate_call_sheet({"production_id": "fayoum", "scenes": []})


# ===========================================================================
# Integration: the whole pipeline, scored against the hand-written answer key
# ===========================================================================


@pytest.fixture(scope="module")
def ch():
    from services.common.mcp_client import ClickHouseMCP, MCPError

    try:
        client = ClickHouseMCP().connect()
        client.run_query("SELECT 1")
    except (MCPError, OSError, FileNotFoundError) as exc:
        pytest.skip(f"no ClickHouse reachable through MCP: {exc}")
    yield client
    client.close()


@pytest.fixture(scope="module")
def answer_key() -> dict:
    return json.loads((FIXTURES / "answer-key.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def gate_run(ch):
    from services.gate import run_gate

    load_production(ch, PRODUCTION)
    call_sheet = json.loads((FIXTURES / "call-sheet.json").read_text(encoding="utf-8"))
    call_sheet["production_id"] = PRODUCTION
    return run_gate(
        call_sheet, ch, pages=ScriptLines.from_path(FIXTURES / "script-v2.fountain")
    )


@pytest.mark.mcp
class TestAgainstTheAnswerKey:
    """SPEC §8, the acceptance criteria, computed rather than asserted by hand."""

    def test_the_planted_second_order_break_is_caught_on_the_first_run(self, gate_run):
        """The revision touched 18 and 31. The damage is in 22, 24 and 33, and a
        text diff says nothing at all about those three."""
        assert [f["scene"] for f in gate_run.findings] == ["22", "24", "33"]

    def test_precision_against_the_answer_key(self, gate_run, answer_key):
        expected = {
            (f["scene"], f["kind"], f["commitment_state"])
            for f in answer_key["expected_findings"]
        }
        produced = [
            (f["scene"], f["kind"], f["commitment_state"]) for f in gate_run.findings
        ]
        correct = [p for p in produced if p in expected]
        precision = len(correct) / len(produced)
        recall = len(set(correct)) / len(expected)

        assert precision >= 0.8, f"precision {precision:.0%}: {produced}"
        assert precision == 1.0, f"a false positive appeared: {set(produced) - expected}"
        assert recall == 1.0, f"missed: {expected - set(correct)}"

    def test_nothing_is_said_about_the_scenes_the_key_expects_silence_on(
        self, gate_run, answer_key
    ):
        """Three false positives kill adoption. Scene 18 is the writer's own edit;
        26 and 27 shoot tomorrow and are fine; 12 and 36 are untouched."""
        silent = {s["scene"] for s in answer_key["expected_silence"]}
        assert silent & {f["scene"] for f in gate_run.findings} == set()

    def test_findings_are_ranked_by_what_has_already_been_paid_for(self, gate_run):
        assert [f["commitment_state"] for f in gate_run.findings] == [
            "shot", "cast", "planned"
        ]
        assert [f["severity"] for f in gate_run.findings] == ["high", "high", "medium"]

    def test_zero_findings_without_evidence(self, gate_run):
        for finding in gate_run.findings:
            assert finding["evidence"], finding["finding_id"]

    def test_every_quoted_line_is_really_on_that_line_of_the_pages(
        self, gate_run, pages_v2
    ):
        """The whole credibility of the product. Re-checked here against the file
        rather than trusted from the database."""
        for finding in gate_run.findings:
            for item in finding["evidence"]:
                if item["type"] != "scene_line":
                    continue
                assert item.get("revision_id", CURRENT_REVISION) == CURRENT_REVISION
                assert pages_v2.quote(item["line"]) == item["quote"]

    def test_the_key_citations_the_answer_key_names_are_all_present(self, gate_run):
        """Not just the right scenes — the right lines."""
        cited = {
            (e["scene"], e["line"])
            for f in gate_run.findings
            for e in f["evidence"]
            if e["type"] == "scene_line"
        }
        assert ("22", 98) in cited     # "She sold it. Was it you who did the papers?"
        assert ("24", 121) in cited    # "You knew she sold Fayoum."
        assert ("33", 207) in cited    # "Seven years back. Spring."
        assert ("31", 180) in cited    # the read, in its new home
        assert ("31", 184) in cited    # "Handwritten, dated MARCH 2022."

    def test_every_finding_cites_a_logged_decision_with_its_reason(
        self, gate_run, answer_key
    ):
        """The thing no competitor stores: WHY, in the words used at the time."""
        seeded = {
            d["reason"]
            for d in json.loads(
                (FIXTURES / "decisions.seed.json").read_text(encoding="utf-8")
            )["decisions"]
        }
        for finding in gate_run.findings:
            reasons = [e["reason"] for e in finding["evidence"] if e["type"] == "decision"]
            assert reasons, f"{finding['scene']} cites no decision"
            assert set(reasons) <= seeded, "a reason was composed rather than cited"

    def test_the_pipeline_ran_all_six_steps_and_kept_its_sql(self, gate_run):
        """SPEC §3: the intermediate state must be visible, not implied."""
        assert [s.name for s in gate_run.steps] == [
            "parse_day", "draft_diff", "traverse", "ledger", "commitment", "rank"
        ]
        ledger = next(s for s in gate_run.steps if s.name == "ledger")
        assert "commitmentRank(" in ledger.sql and "argMax" in ledger.sql, (
            "the frame the demo puts on screen must be the real analytical query"
        )
        assert "GROUP BY" in ledger.sql

    def test_the_diff_is_fact_level_and_finds_the_relocation(self, gate_run):
        diff = next(s for s in gate_run.steps if s.name == "draft_diff")
        assert "RELOCATED 18->31" in diff.detail

    def test_a_production_with_no_graph_loaded_is_refused_not_cleared(self, ch):
        """Silence has to mean silence. "Nothing broke" because nothing was ever
        loaded is a false negative wearing the costume of an all-clear."""
        from services.gate import run_gate

        call_sheet = json.loads(
            (FIXTURES / "call-sheet.json").read_text(encoding="utf-8")
        )
        call_sheet["production_id"] = "test_gate_nothing_loaded"
        with pytest.raises(GateError, match="no scenes on file"):
            run_gate(call_sheet, ch)

    def test_the_run_serialises_for_the_interface(self, gate_run):
        payload = json.loads(json.dumps(gate_run.as_dict()))
        assert payload["scheduled_scenes"] == ["18", "24", "26", "27"]
        assert len(payload["findings"]) == 3
        assert payload["steps"][3]["sql"]


WRITEBACK_PRODUCTION = "test_gate_writeback"


@pytest.fixture(scope="module")
def finding(gate_run) -> dict:
    """The scene 24 finding, which is the one the demo marks intentional."""
    return copy.deepcopy(gate_run.findings[1])


@pytest.mark.mcp
class TestWriteBack:
    """The ledger write, and the findings table. Both idempotent by design.

    Writes land in their own production so that marking a finding intentional
    here can never silence one in the run scored against the answer key — the
    ledger is append-only, so a leaked dismissal would be permanent.
    """

    def test_marking_intentional_writes_one_attributed_ledger_row(self, ch, finding):
        from services.gate import mark_intentional

        reason = "Rana confronts Tarek on suspicion, not on the letter."
        first = mark_intentional(
            ch, WRITEBACK_PRODUCTION, finding, reason, "script.coordinator@fayoum"
        )
        second = mark_intentional(
            ch, WRITEBACK_PRODUCTION, finding, reason, "script.coordinator@fayoum"
        )
        assert second["already_marked"] is True, "a ledger is append-only, not append-twice"
        assert first["decision_id"] == second["decision_id"]

        rows = ch.rows(
            "SELECT decided_by, reason, deviation_reason, intentional_deviation, status "
            f"FROM decisions WHERE production_id = '{WRITEBACK_PRODUCTION}' "
            f"AND decision_id = toUUID('{first['decision_id']}')"
        )
        assert len(rows) == 1, "marking twice must not duplicate the row"
        assert rows[0]["decided_by"] == "script.coordinator@fayoum"
        assert rows[0]["reason"] == reason == rows[0]["deviation_reason"]
        assert rows[0]["intentional_deviation"] == 1

    def test_an_unsigned_or_unexplained_dismissal_is_refused(self, ch, finding):
        from services.gate import mark_intentional

        with pytest.raises(ValueError, match="reason"):
            mark_intentional(ch, WRITEBACK_PRODUCTION, finding, "  ", "someone")
        with pytest.raises(ValueError, match="attributed"):
            mark_intentional(ch, WRITEBACK_PRODUCTION, finding, "because", "")

    def test_the_dismissal_id_is_derived_from_the_finding_not_stored_beside_it(
        self, finding
    ):
        from services.gate import mark_intentional  # noqa: F401  (documents the pair)

        expected = dismissal_id(WRITEBACK_PRODUCTION, finding["finding_id"])
        assert expected == dismissal_id(
            WRITEBACK_PRODUCTION, finding["finding_id"]
        )
        assert expected != dismissal_id(PRODUCTION, finding["finding_id"])

    def test_a_run_persists_its_findings_under_one_run_id(self, ch, gate_run):
        from services.gate import persist_findings

        written = persist_findings(ch, gate_run)
        assert written == 3
        rows = ch.rows(
            "SELECT scene_id, severity, commitment_state, dismissed FROM findings "
            f"WHERE production_id = '{PRODUCTION}' "
            f"AND run_id = toUUID('{gate_run.run_id}') ORDER BY scene_id"
        )
        assert [r["scene_id"] for r in rows] == ["22", "24", "33"]
        assert all(r["dismissed"] == 0 for r in rows)
