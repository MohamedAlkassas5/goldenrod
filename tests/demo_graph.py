"""The demo graph: both fixture revisions, extracted and loaded for a test.

Shared by tests/test_gate.py and tests/test_api.py, which both need a production
whose graph is really in ClickHouse — not a mock of one. The Extractor's model
pass is stood in for by a scripted backend, exactly as in tests/test_extractor.py;
everything else is the real parse, the real identity resolution and the real
ingest through the MCP server.
"""

from __future__ import annotations

import copy
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[1] / "data" / "fixtures"

PRIOR_REVISION = "white-2026-08-01"
CURRENT_REVISION = "goldenrod-2026-08-29"


def _letter_read(line: int, quote: str) -> dict:
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


def _letter_date(line: int, quote: str, year: int) -> dict:
    return {
        "statement": f"The letter is dated March {year}",
        "kind": "temporal",
        "source_line": line,
        "quote": quote,
        "entities": [{"name": "the letter", "type": "prop"}],
    }


# The white pages: the letter is read in 18, and dated there too.
V1_PAYLOADS = {
    "18": {
        "facts": [
            _letter_read(78, "She opens the letter and reads it."),
            _letter_date(82, "Handwritten, dated MARCH 2019.", 2019),
        ],
        "knowledge_state": [
            {"character": "RANA", "fact_ref": "#0", "knows": True,
             "acquired_via": "reads it in 18"}
        ],
        "dependencies": [],
    },
    "22": {
        "facts": [],
        "knowledge_state": [
            {"character": "RANA", "fact_ref": "f18.1", "knows": True,
             "acquired_via": "read it in 18"}
        ],
        "dependencies": [
            {"fact_ref": "f18.1", "kind": "references", "evidence_line": 106,
             "evidence_quote": "She sold it. Was it you who did the papers?"}
        ],
    },
    "24": {
        "facts": [],
        "knowledge_state": [],
        "dependencies": [
            {"fact_ref": "f18.1", "kind": "references", "evidence_line": 129,
             "evidence_quote": "You knew she sold Fayoum."}
        ],
    },
    "33": {
        "facts": [],
        "knowledge_state": [],
        "dependencies": [
            {"fact_ref": "f18.2", "kind": "assumes", "evidence_line": 203,
             "evidence_quote": "Seven years back. Spring."}
        ],
    },
}

# The goldenrod revision: both facts now live in 31, and the date changed.
# Note what is NOT here: scenes 22 and 24 have no dependency any more. They come
# before 31, and the Extractor cannot express a forward reference. That silence
# is the break, and carrying those edges forward is how the Gate finds it.
V2_PAYLOADS = {
    "31": {
        "facts": [
            _letter_read(
                180,
                "RANA takes the letter out of her jacket. It is still sealed. "
                "She opens it here.",
            ),
            _letter_date(184, "Handwritten, dated MARCH 2022.", 2022),
        ],
        "knowledge_state": [
            {"character": "RANA", "fact_ref": "#0", "knows": True,
             "acquired_via": "opens it at the grove"}
        ],
        "dependencies": [],
    },
    "33": {
        "facts": [],
        "knowledge_state": [],
        "dependencies": [
            {"fact_ref": "f31.2", "kind": "assumes", "evidence_line": 207,
             "evidence_quote": "Seven years back. Spring."}
        ],
    },
}


class ScriptedBackend:
    """Stands in for the model, exactly as in tests/test_extractor.py."""

    name = "scripted"

    def __init__(self, payloads: dict[str, dict]):
        self.payloads = payloads

    def extract_scene(self, request):
        return copy.deepcopy(
            self.payloads.get(
                request.scene.scene_number,
                {"facts": [], "knowledge_state": [], "dependencies": []},
            )
        )


def load_production(ch, production: str) -> None:
    """Both revisions of the fixture screenplay, plus the fixture ledger."""
    from services.extractor import extract_graph, parse_fountain
    from services.loader.ingest import ingest_graph
    from services.loader.seed import load_seed_files, seed_ledger

    for name, revision, payloads in (
        ("script-v1.fountain", PRIOR_REVISION, V1_PAYLOADS),
        ("script-v2.fountain", CURRENT_REVISION, V2_PAYLOADS),
    ):
        script = parse_fountain((FIXTURES / name).read_text(encoding="utf-8"))
        graph, _ = extract_graph(script, production, revision, ScriptedBackend(payloads))
        ingest_graph(graph, ch)

    seed = copy.deepcopy(load_seed_files(FIXTURES))
    seed["production_id"] = production
    seed_ledger(seed, ch)
