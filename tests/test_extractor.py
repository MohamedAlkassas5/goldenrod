"""The Extractor: parsing, entity identity, and the citation rule.

The model is the only part of the Extractor that cannot be tested here, so the
seam is drawn so that it is also the only part that is not. Everything else —
the Fountain parser, entity identity, the check that a quote is actually on the
line it cites, graph assembly — runs against the real fixture screenplays with a
scripted backend standing in for the model. The code under test cannot tell.

The scripted payloads are deliberately written the way a model answers: some
right, some citing the wrong line, some referencing a fact that does not exist.
What matters is what survives.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from services.extractor import (
    EntityRegistry,
    ExtractionError,
    GeminiBackend,
    StructureOnlyBackend,
    entity_id,
    extract_graph,
    parse_fountain,
)
from services.extractor.entities import location_name
from services.extractor.fountain import FountainError, cue_name, numbered, parse_heading
from services.extractor.prompt import (
    DEPENDENCY_KINDS,
    FACT_KINDS,
    SCENE_SCHEMA,
    SYSTEM_PROMPT,
    build_prompt,
)
from services.loader.identity import fact_key, fact_match_key
from services.loader.ingest import resolve

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "data" / "fixtures"
CONTRACTS = ROOT / "contracts"

PRODUCTION = "fayoum"
CURRENT_REVISION = "goldenrod-2026-08-29"
PRIOR_REVISION = "white-2026-08-01"


@pytest.fixture(scope="module")
def script_v1():
    return parse_fountain((FIXTURES / "script-v1.fountain").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def script_v2():
    return parse_fountain((FIXTURES / "script-v2.fountain").read_text(encoding="utf-8"))


# ===========================================================================
# The parser. Structure is parsed, never inferred.
# ===========================================================================
def test_every_scene_is_found_with_its_locked_number(script_v2):
    assert [s.scene_number for s in script_v2.scenes] == [
        "1", "5", "8", "12", "18", "22", "24", "26", "27", "31", "33", "36", "40", "44"
    ]


def test_scene_numbers_are_identical_across_the_revision(script_v1, script_v2):
    """Locked pages. If this ever fails, fact identity has no coordinate system."""
    assert [s.scene_number for s in script_v1.scenes] == [
        s.scene_number for s in script_v2.scenes
    ]


def test_slugline_fields_are_read_not_guessed(script_v2):
    scene = script_v2.scene("31")
    assert (scene.int_ext, scene.day_night, scene.location) == ("EXT", "DAY", "FAYOUM LAND")
    kitchen = script_v2.scene("22")
    assert (kitchen.int_ext, kitchen.day_night, kitchen.location) == (
        "INT", "NIGHT", "FLAT, KITCHEN",
    )


def test_a_location_with_a_dash_keeps_its_whole_name(script_v2):
    assert script_v2.scene("5").location == "FLAT, HALLWAY - ZAMALEK"
    assert script_v2.scene("5").day_night == "DAY"


def test_line_numbers_are_the_real_ones(script_v2):
    """Answer-key citations are file line numbers. So are ours."""
    assert script_v2.line_text(98).strip() == "She sold it. Was it you who did the papers?"
    assert script_v2.scene("22").start_line == 82
    assert script_v2.scene("22").end_line == 111


def test_dialogue_is_attributed_to_the_cue_above_it(script_v2):
    lines = [e.text for e in script_v2.scene("33").dialogue_of("HAMDY")]
    assert "Seven years back. Spring." in lines


def test_parentheticals_are_not_dialogue(script_v2):
    kinds = {e.type for e in script_v2.scene("31").elements if e.line == 193}
    assert kinds == {"parenthetical"}


def test_insert_and_back_to_scene_are_not_characters(script_v2):
    """Both are uppercase and both are followed by a blank line. Neither is cast."""
    assert script_v2.scene("31").characters == ["RANA"]
    texts = {e.text for e in script_v2.scene("31").elements if e.type == "action"}
    assert "INSERT - THE LETTER" in texts and "BACK TO SCENE" in texts


def test_speaking_parts_are_listed_in_order_of_appearance(script_v2):
    assert script_v2.scene("22").characters == ["YOUSSEF", "RANA"]


def test_title_page_is_read(script_v2):
    assert script_v2.title == "The Fayoum Letter"
    assert script_v2.title_page["draft date"] == "2026-08-29"


def test_numbered_view_uses_file_line_numbers(script_v2):
    block = numbered(script_v2, script_v2.scene("33"))
    assert block.splitlines()[0].startswith("196| INT. FARMHOUSE - DAY #33#")
    assert "207| Seven years back. Spring." in block


def test_an_unnumbered_slugline_is_refused():
    with pytest.raises(FountainError, match="without a scene number"):
        parse_fountain("INT. FLAT - DAY #1#\n\nAction.\n\nEXT. STREET - NIGHT\n\nMore.\n")


def test_a_duplicate_scene_number_is_refused():
    with pytest.raises(FountainError, match="duplicate scene numbers"):
        parse_fountain("INT. FLAT - DAY #4#\n\nA.\n\nEXT. STREET - NIGHT #4#\n\nB.\n")


def test_a_script_with_no_sluglines_is_refused():
    with pytest.raises(FountainError, match="no numbered sluglines"):
        parse_fountain("Title: Nothing\n\n====\n\nJust prose.\n")


@pytest.mark.parametrize(
    "heading, expected",
    [
        ("INT. FLAT, KITCHEN - NIGHT #22#", ("22", "INT", "NIGHT", "FLAT, KITCHEN")),
        ("EXT. GROVE - DUSK #36A#", ("36A", "EXT", "DUSK", "GROVE")),
        ("INT./EXT. CAR - DAY #7#", ("7", "INT/EXT", "DAY", "CAR")),
        ("EST. CITY - DAWN #2#", ("2", "EXT", "DAWN", "CITY")),
        ("INT. ROOM #9#", ("9", "INT", "", "ROOM")),
    ],
)
def test_heading_forms(heading, expected):
    assert parse_heading(heading) == expected


def test_a_line_that_is_not_a_slugline_is_not_one():
    assert parse_heading("INSERT - THE LETTER") is None
    assert parse_heading("INTERIOR DESIGNER arrives.") is None


def test_cue_name_drops_the_modifier():
    assert cue_name("MRS. WADIDA (CONT'D)") == "MRS. WADIDA"
    assert cue_name("TAREK (V.O.)") == "TAREK"


# ===========================================================================
# Entity identity — the join between the graph and the money.
# ===========================================================================
@pytest.mark.parametrize(
    "name, expected",
    [
        ("RANA", "entity:rana"),
        ("the letter", "entity:letter"),
        ("The Letter", "entity:letter"),
        ("TIN BOX", "entity:tin-box"),
        ("the Fayoum land", "entity:fayoum-land"),
        ("MRS. WADIDA", "entity:mrs-wadida"),
        ("A KEY", "entity:key"),
    ],
)
def test_entity_ids_are_derived_by_rule(name, expected):
    assert entity_id(name) == expected


def test_the_seeded_ledger_ids_are_the_ids_the_extractor_derives():
    """If these drift, the commitment lookup misses and everything ranks 'none'."""
    seeded = json.loads((FIXTURES / "commitments.seed.json").read_text(encoding="utf-8"))
    names = {
        "entity:rana": "RANA",
        "entity:tarek": "TAREK",
        "entity:hamdy": "HAMDY",
        "entity:letter": "the letter",
        "entity:tin-box": "TIN BOX",
        "entity:flat": "FLAT",
        "entity:cafe": "CAFE",
        "entity:street": "STREET",
        "entity:farmhouse": "FARMHOUSE",
        "entity:fayoum-land": "FAYOUM LAND",
        "entity:cairo-airport": "CAIRO AIRPORT",
    }
    for wanted, name in names.items():
        assert entity_id(name) == wanted
    assert {c["entity_id"] for c in seeded["commitments"]} <= set(names)


def test_a_sub_location_commits_to_the_practical_not_the_room():
    """FLAT, KITCHEN and FLAT, LIVING ROOM are one agreement and one access window."""
    assert location_name("FLAT, KITCHEN") == "FLAT"
    assert location_name("FLAT, HALLWAY - ZAMALEK") == "FLAT"
    assert location_name("FAYOUM LAND") == "FAYOUM LAND"


def test_a_fuller_name_in_an_action_line_resolves_to_the_cue():
    registry = EntityRegistry()
    rana = registry.register("RANA", "character")
    assert registry.resolve("Rana Mostafa", "character") is rana
    assert "Rana Mostafa" in rana.aliases or registry.resolve("Rana Mostafa") is rana


def test_an_honorific_cannot_swallow_a_different_character():
    registry = EntityRegistry()
    registry.register("MRS. WADIDA", "character")
    registry.register("MRS. HANAA", "character")
    assert entity_id("MRS. WADIDA") != entity_id("MRS. HANAA")
    assert len(registry) == 2


def test_a_name_already_billed_as_a_location_is_not_forked_into_a_second_entity():
    """The grove in the slugline and "the grove" in a fact are one thing, and the
    production office logged its commitment against one id. Splitting them would
    put a finding on an entity nothing has been paid for."""
    registry = EntityRegistry()
    registry.register("OLIVE GROVE", "location")
    again = registry.register("the olive grove", "symbol")
    assert len(registry) == 1
    assert again.entity_id == "entity:olive-grove"
    assert again.type == "location", "the slugline is the more reliable source"


def test_a_type_outside_the_contract_is_refused():
    with pytest.raises(ValueError, match="not in contracts/graph.schema.json"):
        EntityRegistry().register("the letter", "macguffin")


def test_a_nameless_entity_is_refused():
    with pytest.raises(ValueError, match="no usable identity"):
        EntityRegistry().register("   ", "prop")


def test_aliases_accumulate_across_scenes(script_v2):
    graph, _ = extract_graph(script_v2, PRODUCTION, CURRENT_REVISION, StructureOnlyBackend())
    flat = next(e for e in graph["entities"] if e["entity_id"] == "entity:flat")
    assert "FLAT, KITCHEN" in flat["aliases"]
    assert "FLAT, LIVING ROOM" in flat["aliases"]


# ===========================================================================
# The scripted backend — a model that is right, wrong and vague in turn.
# ===========================================================================
class ScriptedBackend:
    """Returns a canned payload per scene number, like a model would."""

    name = "scripted"

    def __init__(self, payloads: dict[str, dict]):
        self.payloads = payloads
        self.seen: list[str] = []
        self.prompts: list[str] = []

    def extract_scene(self, request):
        self.seen.append(request.scene.scene_number)
        self.prompts.append(
            build_prompt(
                request.script, request.scene, request.known_facts, request.known_entities
            )
        )
        return copy.deepcopy(
            self.payloads.get(
                request.scene.scene_number,
                {"facts": [], "knowledge_state": [], "dependencies": []},
            )
        )


def _letter_fact(line: int, quote: str) -> dict:
    return {
        "statement": "Rana has read her mother's letter and knows the Fayoum land was sold",
        "kind": "knowledge",
        "source_line": line,
        "quote": quote,
        "entities": [
            {"name": "RANA", "type": "character"},
            {"name": "the letter", "type": "prop"},
        ],
    }


V1_PAYLOADS = {
    # The letter is read in 18, as originally written.
    "18": {
        "facts": [_letter_fact(78, "She opens the letter and reads it.")],
        "knowledge_state": [
            {"character": "RANA", "fact_ref": "#0", "knows": True, "acquired_via": "reads it in 18"}
        ],
        "dependencies": [],
    },
    "22": {
        "facts": [],
        "knowledge_state": [
            {"character": "RANA", "fact_ref": "f18.1", "knows": True, "acquired_via": "read it in 18"}
        ],
        "dependencies": [
            {
                "fact_ref": "f18.1",
                "kind": "references",
                "evidence_line": 106,
                "evidence_quote": "She sold it. Was it you who did the papers?",
            }
        ],
    },
    "24": {
        "facts": [],
        "knowledge_state": [],
        "dependencies": [
            {
                "fact_ref": "f18.1",
                "kind": "references",
                "evidence_line": 129,
                "evidence_quote": "You knew she sold Fayoum.",
            }
        ],
    },
}

V2_PAYLOADS = {
    # The goldenrod revision: 18 no longer opens it, 31 does.
    "31": {
        "facts": [
            _letter_fact(
                180, "RANA takes the letter out of her jacket. It is still sealed. She opens it here."
            )
        ],
        "knowledge_state": [
            {"character": "RANA", "fact_ref": "#0", "knows": True, "acquired_via": "opens it at the grove"}
        ],
        "dependencies": [],
    },
    "33": {
        "facts": [],
        "knowledge_state": [],
        "dependencies": [
            {
                "fact_ref": "f31.1",
                "kind": "assumes",
                "evidence_line": 207,
                "evidence_quote": "Seven years back. Spring.",
            }
        ],
    },
}


# ===========================================================================
# Citation enforcement — CLAUDE.md rule 1, biting before a finding exists.
# ===========================================================================
def _one_scene(script, scene_number: str, payload: dict):
    backend = ScriptedBackend({scene_number: payload})
    return extract_graph(script, PRODUCTION, CURRENT_REVISION, backend)


def test_a_fact_whose_quote_is_not_on_the_cited_line_is_dropped(script_v2):
    graph, report = _one_scene(
        script_v2,
        "18",
        {
            "facts": [_letter_fact(78, "She reads the letter and weeps.")],
            "knowledge_state": [],
            "dependencies": [],
        },
    )
    assert graph["facts"] == []
    assert any("not on the line it cites" in d.reason for d in report.dropped)


def test_a_fact_citing_a_line_outside_its_scene_is_dropped(script_v2):
    graph, report = _one_scene(
        script_v2,
        "18",
        {
            "facts": [_letter_fact(207, "Seven years back. Spring.")],
            "knowledge_state": [],
            "dependencies": [],
        },
    )
    assert graph["facts"] == []
    assert report.dropped


def test_a_quote_on_the_wrong_line_of_the_right_scene_is_corrected(script_v2):
    """The one repair: the evidence still comes from the file, only the arithmetic
    is fixed. Ambiguous or absent and it would be dropped instead."""
    graph, report = _one_scene(
        script_v2,
        "18",
        {
            "facts": [_letter_fact(72, "She turns the letter over. She does not open it.")],
            "knowledge_state": [],
            "dependencies": [],
        },
    )
    assert graph["facts"][0]["source_line"] == 78
    assert report.snapped == 1
    assert report.dropped == []


def test_a_fact_kind_outside_the_contract_is_dropped(script_v2):
    fact = _letter_fact(78, "She turns the letter over. She does not open it.")
    fact["kind"] = "vibe"
    graph, report = _one_scene(
        script_v2, "18", {"facts": [fact], "knowledge_state": [], "dependencies": []}
    )
    assert graph["facts"] == []
    assert any("kind is not in the contract" in d.reason for d in report.dropped)


def test_a_fact_about_nothing_is_dropped(script_v2):
    fact = _letter_fact(78, "She turns the letter over. She does not open it.")
    fact["entities"] = []
    graph, report = _one_scene(
        script_v2, "18", {"facts": [fact], "knowledge_state": [], "dependencies": []}
    )
    assert graph["facts"] == []
    assert any("no resolvable entity" in d.reason for d in report.dropped)


def test_an_entity_with_an_impossible_type_does_not_stop_the_pass(script_v2):
    fact = _letter_fact(78, "She turns the letter over. She does not open it.")
    fact["entities"] = [
        {"name": "the letter", "type": "macguffin"},
        {"name": "RANA", "type": "character"},
    ]
    graph, report = _one_scene(
        script_v2, "18", {"facts": [fact], "knowledge_state": [], "dependencies": []}
    )
    assert graph["facts"][0]["entity_ids"] == ["entity:rana"]
    assert any(d.item == "entity" for d in report.dropped)


def test_knowledge_state_pointing_at_a_missing_fact_is_dropped(script_v2):
    graph, report = _one_scene(
        script_v2,
        "22",
        {
            "facts": [],
            "knowledge_state": [
                {"character": "RANA", "fact_ref": "f99.1", "knows": True, "acquired_via": ""}
            ],
            "dependencies": [],
        },
    )
    assert graph["knowledge_state"] == []
    assert any("fact that does not exist" in d.reason for d in report.dropped)


def test_knowledge_state_about_someone_who_is_not_a_character_is_dropped(script_v2):
    graph, report = _one_scene(
        script_v2,
        "18",
        {
            "facts": [_letter_fact(78, "She turns the letter over. She does not open it.")],
            "knowledge_state": [
                {"character": "the letter", "fact_ref": "#0", "knows": True, "acquired_via": ""}
            ],
            "dependencies": [],
        },
    )
    assert graph["knowledge_state"] == []
    assert any("not a known character" in d.reason for d in report.dropped)


def test_a_dropped_fact_does_not_shift_the_index_of_the_next_one(script_v2):
    """`#1` must still mean the model's second fact after its first is refused."""
    bad = _letter_fact(78, "A line that is not in this scene at all.")
    good = {
        "statement": "Rana keeps the sealed letter in her jacket",
        "kind": "possession",
        "source_line": 80,
        "quote": "She puts it in her jacket pocket and closes the tin box.",
        "entities": [{"name": "RANA", "type": "character"}, {"name": "the letter", "type": "prop"}],
    }
    graph, _ = _one_scene(
        script_v2,
        "18",
        {
            "facts": [bad, good],
            "knowledge_state": [
                {"character": "RANA", "fact_ref": "#1", "knows": True, "acquired_via": "keeps it"}
            ],
            "dependencies": [],
        },
    )
    assert len(graph["facts"]) == 1
    assert graph["knowledge_state"][0]["fact_id"] == graph["facts"][0]["fact_id"]


def test_a_dependency_on_a_fact_from_its_own_scene_is_dropped(script_v2):
    """Second-order or nothing: a scene leaning on its own fact tells nobody
    anything, and it is exactly the kind of noise that gets a tool switched off."""
    graph, report = _one_scene(
        script_v2,
        "18",
        {
            "facts": [_letter_fact(78, "She turns the letter over. She does not open it.")],
            "knowledge_state": [],
            "dependencies": [
                {
                    "fact_ref": "#0",
                    "kind": "references",
                    "evidence_line": 80,
                    "evidence_quote": "She puts it in her jacket pocket and closes the tin box.",
                }
            ],
        },
    )
    assert graph["dependencies"] == []
    assert any("its own scene" in d.reason for d in report.dropped)


def test_a_dependency_with_unverifiable_evidence_is_dropped(script_v1):
    payloads = copy.deepcopy(V1_PAYLOADS)
    payloads["24"]["dependencies"][0]["evidence_quote"] = "You never told me about Fayoum."
    graph, report = extract_graph(
        script_v1, PRODUCTION, PRIOR_REVISION, ScriptedBackend(payloads)
    )
    assert [d["to_scene_id"] for d in graph["dependencies"]] == ["sc22"]
    assert any("evidence quote is not on the line" in d.reason for d in report.dropped)


def test_a_dependency_kind_outside_the_contract_is_dropped(script_v1):
    payloads = copy.deepcopy(V1_PAYLOADS)
    payloads["24"]["dependencies"][0]["kind"] = "vaguely relates to"
    graph, report = extract_graph(
        script_v1, PRODUCTION, PRIOR_REVISION, ScriptedBackend(payloads)
    )
    assert [d["to_scene_id"] for d in graph["dependencies"]] == ["sc22"]
    assert any("kind is not in the contract" in d.reason for d in report.dropped)


def test_the_stored_quote_comes_from_the_file_not_from_the_model(script_v1):
    """Whatever the model paraphrased, what is written down is the line."""
    payloads = copy.deepcopy(V1_PAYLOADS)
    payloads["24"]["dependencies"][0]["evidence_quote"] = "you knew she sold fayoum"
    graph, _ = extract_graph(script_v1, PRODUCTION, PRIOR_REVISION, ScriptedBackend(payloads))
    quote = next(d for d in graph["dependencies"] if d["to_scene_id"] == "sc24")["evidence_quote"]
    assert quote == "You knew she sold Fayoum."


# ===========================================================================
# The graph it produces
# ===========================================================================
def test_the_graph_is_accepted_by_the_loader(script_v1):
    graph, _ = extract_graph(script_v1, PRODUCTION, PRIOR_REVISION, ScriptedBackend(V1_PAYLOADS))
    rows = resolve(graph)
    assert len(rows["scenes"]) == 14
    assert rows["facts"][0]["established_in_scene_number"] == "18"
    assert {r["to_scene_number"] for r in rows["dependencies"]} == {"22", "24"}


def test_every_scene_is_written_even_when_it_yields_no_fact(script_v2):
    graph, report = extract_graph(
        script_v2, PRODUCTION, CURRENT_REVISION, StructureOnlyBackend()
    )
    assert len(graph["scenes"]) == report.scenes == 14
    assert graph["facts"] == []


def test_scenes_are_visited_in_page_order(script_v2):
    backend = ScriptedBackend({})
    extract_graph(script_v2, PRODUCTION, CURRENT_REVISION, backend)
    assert backend.seen == [s.scene_number for s in script_v2.scenes]


def test_a_scene_is_shown_only_the_facts_established_before_it(script_v1):
    backend = ScriptedBackend(V1_PAYLOADS)
    extract_graph(script_v1, PRODUCTION, PRIOR_REVISION, backend)
    prompts = dict(zip(backend.seen, backend.prompts))
    assert "f18.1" not in prompts["12"], "a scene must not see a later scene's fact"
    assert "f18.1" in prompts["24"]


def test_the_planted_relocation_survives_the_extractor(script_v1, script_v2):
    """End to end on the real pages: the same fact, established in 18 under the
    white draft and in 31 under the goldenrod revision. Its match key holds, its
    fact key moves — which is exactly what the tier-2 diff looks for."""
    v1, _ = extract_graph(script_v1, PRODUCTION, PRIOR_REVISION, ScriptedBackend(V1_PAYLOADS))
    v2, _ = extract_graph(script_v2, PRODUCTION, CURRENT_REVISION, ScriptedBackend(V2_PAYLOADS))

    before = resolve(v1)["facts"][0]
    after = resolve(v2)["facts"][0]

    assert before["established_in_scene_number"] == "18"
    assert after["established_in_scene_number"] == "31"
    assert fact_match_key(PRODUCTION, before["kind"], before["entity_ids"]) == fact_match_key(
        PRODUCTION, after["kind"], after["entity_ids"]
    )
    assert fact_key(
        PRODUCTION, before["kind"], "18", before["entity_ids"], 1
    ) != fact_key(PRODUCTION, after["kind"], "31", after["entity_ids"], 1)


def test_the_second_order_scenes_are_untouched_but_dependent(script_v1):
    """22 and 24 are byte-identical across the revision and still the ones at risk."""
    graph, _ = extract_graph(script_v1, PRODUCTION, PRIOR_REVISION, ScriptedBackend(V1_PAYLOADS))
    dependents = {d["to_scene_id"]: d for d in graph["dependencies"]}
    assert set(dependents) == {"sc22", "sc24"}
    assert dependents["sc22"]["evidence_line"] == 106
    assert dependents["sc24"]["evidence_line"] == 129


def test_the_letter_read_is_recorded_as_knowledge(script_v1):
    graph, _ = extract_graph(script_v1, PRODUCTION, PRIOR_REVISION, ScriptedBackend(V1_PAYLOADS))
    entry = next(k for k in graph["knowledge_state"] if k["scene_id"] == "sc18")
    assert entry["character_entity_id"] == "entity:rana"
    assert entry["knows"] is True


def test_extraction_is_deterministic_for_the_same_input(script_v1):
    a, _ = extract_graph(script_v1, PRODUCTION, PRIOR_REVISION, ScriptedBackend(V1_PAYLOADS))
    b, _ = extract_graph(script_v1, PRODUCTION, PRIOR_REVISION, ScriptedBackend(V1_PAYLOADS))
    assert a == b


def test_scene_text_hash_changes_only_where_the_pages_changed(script_v1, script_v2):
    v1, _ = extract_graph(script_v1, PRODUCTION, PRIOR_REVISION, StructureOnlyBackend())
    v2, _ = extract_graph(script_v2, PRODUCTION, CURRENT_REVISION, StructureOnlyBackend())
    before = {s["scene_number"]: s["text_hash"] for s in v1["scenes"]}
    after = {s["scene_number"]: s["text_hash"] for s in v2["scenes"]}
    assert {n for n in before if before[n] != after[n]} == {"18", "31"}


def test_a_graph_that_would_not_load_is_refused(script_v2, monkeypatch):
    """The Extractor validates its own output before returning it."""
    import services.extractor.extract as extract_module

    backend = ScriptedBackend(
        {"18": {"facts": [_letter_fact(78, "She turns the letter over. She does not open it.")],
                "knowledge_state": [], "dependencies": []}}
    )
    original = extract_module._synopsis
    monkeypatch.setattr(extract_module, "_synopsis", lambda scene: "x" * 400)
    try:
        with pytest.raises(ExtractionError, match="the loader will not accept"):
            extract_graph(script_v2, PRODUCTION, CURRENT_REVISION, backend)
    finally:
        monkeypatch.setattr(extract_module, "_synopsis", original)


# ===========================================================================
# The prompt and the fixed schema
# ===========================================================================
def test_the_response_schema_uses_the_contract_enums():
    """The model is constrained to what the graph contract already allows."""
    contract = json.loads((CONTRACTS / "graph.schema.json").read_text(encoding="utf-8"))
    facts = contract["properties"]["facts"]["items"]["properties"]
    dependencies = contract["properties"]["dependencies"]["items"]["properties"]
    entities = contract["properties"]["entities"]["items"]["properties"]

    assert list(FACT_KINDS) == facts["kind"]["enum"]
    assert list(DEPENDENCY_KINDS) == dependencies["kind"]["enum"]
    assert (
        SCENE_SCHEMA["properties"]["facts"]["items"]["properties"]["entities"]["items"][
            "properties"
        ]["type"]["enum"]
        == entities["type"]["enum"]
    )


def test_the_schema_requires_a_citation_on_every_claim():
    fact = SCENE_SCHEMA["properties"]["facts"]["items"]
    dependency = SCENE_SCHEMA["properties"]["dependencies"]["items"]
    assert {"source_line", "quote"} <= set(fact["required"])
    assert {"evidence_line", "evidence_quote"} <= set(dependency["required"])


def test_the_system_prompt_states_the_rules_that_the_code_enforces():
    assert "CITE OR DROP" in SYSTEM_PROMPT
    assert "PRECISION OVER RECALL" in SYSTEM_PROMPT
    assert "%" not in SYSTEM_PROMPT, "no confidence percentages, ever"


def test_no_confidence_percentage_anywhere_in_the_extractor():
    """CLAUDE.md rule 2. Raw counts or nothing."""
    import re

    package = ROOT / "services" / "extractor"
    offenders = [
        str(path.relative_to(ROOT))
        for path in package.rglob("*.py")
        if re.search(r"\bconfidence[_ ]?(score|pct|percent)", path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_the_prompt_shows_the_scene_with_its_real_line_numbers(script_v2):
    prompt = build_prompt(script_v2, script_v2.scene("33"), [], [])
    assert "207| Seven years back. Spring." in prompt
    assert "SCENE 33" in prompt


def test_the_prompt_lists_established_facts_by_handle(script_v2):
    known = [
        {
            "handle": "f18.1",
            "kind": "knowledge",
            "scene_number": "18",
            "statement": "Rana has read the letter",
            "entity_names": ["RANA", "the letter"],
        }
    ]
    prompt = build_prompt(script_v2, script_v2.scene("24"), known, [])
    assert "f18.1" in prompt and "Rana has read the letter" in prompt


# ===========================================================================
# The Gemini backend, without a network
# ===========================================================================
def test_gemini_decodes_a_fenced_response():
    backend = GeminiBackend(client=object())
    payload, error = backend._decode(
        '```json\n{"facts": [], "knowledge_state": [], "dependencies": []}\n```'
    )
    assert error == "" and payload == {"facts": [], "knowledge_state": [], "dependencies": []}


def test_gemini_rejects_a_response_that_misses_the_schema():
    backend = GeminiBackend(client=object())
    payload, error = backend._decode('{"facts": []}')
    assert payload is None and "knowledge_state" in error


def test_gemini_rejects_non_json():
    backend = GeminiBackend(client=object())
    payload, error = backend._decode("Here are the facts I found:")
    assert payload is None and "not JSON" in error


def test_gemini_model_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test-model")
    assert GeminiBackend(client=object()).model == "gemini-test-model"
    assert GeminiBackend("explicit", client=object()).model == "explicit"


# ===========================================================================
# Integration: the Extractor's output, written through the MCP server.
# Skipped automatically when no ClickHouse is reachable.
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


@pytest.mark.mcp
class TestExtractorOutputAgainstClickHouse:
    """The boundary that matters: does what the Extractor emits actually load?

    `resolve()` proves the shape. Only a real write proves that the database's
    MATERIALIZED fact_key agrees with the identity the loader computed from the
    Extractor's entity ids — which is the whole basis of the fact-level diff.
    """

    PRODUCTION = "test_extractor"

    def _graph(self, script, revision, payloads):
        graph, _ = extract_graph(script, self.PRODUCTION, revision, ScriptedBackend(payloads))
        return graph

    def test_an_extracted_revision_ingests_and_verifies(self, ch, script_v1):
        from services.loader.ingest import ingest_graph

        result = ingest_graph(self._graph(script_v1, PRIOR_REVISION, V1_PAYLOADS), ch)
        assert result["verified"] is True
        assert result["written"]["scenes"] == 14
        assert result["written"]["dependencies"] == 2

    def test_the_relocation_shows_up_in_the_draft_diff_query(self, ch, script_v1, script_v2):
        """The planted break, end to end: two extracted revisions of the real
        pages, diffed by the reference query in schema.sql."""
        from services.common.queries import bind, get_query
        from services.loader.ingest import ingest_graph

        ingest_graph(self._graph(script_v1, PRIOR_REVISION, V1_PAYLOADS), ch)
        ingest_graph(self._graph(script_v2, CURRENT_REVISION, V2_PAYLOADS), ch)

        rows = ch.rows(
            bind(
                get_query("draft_diff"),
                production=self.PRODUCTION,
                prior_revision=PRIOR_REVISION,
                current_revision=CURRENT_REVISION,
            )
        )
        relocations = [r for r in rows if r["change"] == "RELOCATED"]
        assert len(relocations) == 1
        assert (relocations[0]["was_scene"], relocations[0]["now_scene"]) == ("18", "31")
