"""Evidence: the part of a finding that has to be true.

CLAUDE.md rule 1, verbatim: "No finding without evidence. Every finding must
carry at least one evidence object (scene + line, or a decision id). Enforce
this in code, not in a prompt. Drop findings that fail the check."

So nothing here composes evidence. Every object is copied out of something that
already exists:

    scene_line   the dependency's own verified quote, or a line read back out of
                 the pages. The Extractor checked the first against the file at
                 extraction time and dropped it if it did not hold; the second is
                 read from the file here, at the line the graph recorded.
    decision     a row of the ledger, with the reason given at the time.
    commitment   a row of commitment state, with what has been paid for.

WHY THE PAGES ARE AN ARGUMENT
-----------------------------
A fact's `source_line` is in the graph but the line's TEXT is not — `facts`
stores the model's statement, which is a paraphrase and is not admissible as a
quote. So the Gate reads the revision's Fountain file to turn `source_line` into
the words actually on the page.

If the pages are not supplied, that citation is simply omitted. It is never
filled in from the statement: an invented quote is worse than a missing one, and
the finding still stands on the dependency's own evidence, which was verified
against the file when the graph was built.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from services.common.text import normalise

MAX_QUOTE = 300  # contracts/finding.schema.json

# A quote shorter than this is not allowed to identify a line on its own.
# Mirrors MIN_SNAP_QUOTE in services/extractor/extract.py, for the same reason:
# "What?" appears on four pages and identifies nothing.
MIN_LOCATING_QUOTE = 6


class ScriptLines:
    """One revision's pages, addressable by line number.

    Line numbers are the contract (see services/extractor/fountain.py): a `line`
    in the graph is a 1-based index into the file exactly as it was extracted,
    so the same index read back here is the same line.
    """

    def __init__(self, lines: list[str], name: str = ""):
        self._lines = lines
        self.name = name

    @classmethod
    def from_text(cls, text: str, name: str = "") -> "ScriptLines":
        return cls(text.replace("\r\n", "\n").replace("\r", "\n").split("\n"), name)

    @classmethod
    def from_path(cls, path: Path) -> "ScriptLines":
        return cls.from_text(Path(path).read_text(encoding="utf-8"), Path(path).name)

    def __len__(self) -> int:
        return len(self._lines)

    def quote(self, line: int) -> str | None:
        """The text at a 1-based line, or None if the file does not have it."""
        if not 1 <= int(line or 0) <= len(self._lines):
            return None
        text = self._lines[int(line) - 1].strip()
        return text[:MAX_QUOTE] or None

    def find_unique(self, quote: str) -> int | None:
        """The one line these words are on, or None if it is not exactly one.

        Used to carry a citation across a revision. The quote came out of the
        prior revision's pages and was verified there; a revision re-flows line
        numbers, so the line it is on now has to be found rather than assumed.

        An exact line match is tried first and is what normally hits, because the
        Extractor stores the whole line as the quote. Containment is the fallback
        for a truncated one. Either way the match must be unique — an ambiguous
        citation is not one this project prints. Compare `locate` in
        services/extractor/extract.py, which is the same discipline applied
        inside a single scene.
        """
        wanted = normalise(quote)
        if len(wanted) < MIN_LOCATING_QUOTE:
            return None
        exact = [n for n, raw in enumerate(self._lines, 1) if normalise(raw) == wanted]
        if len(exact) == 1:
            return exact[0]
        if exact:
            return None
        loose = [n for n, raw in enumerate(self._lines, 1) if wanted in normalise(raw)]
        return loose[0] if len(loose) == 1 else None


def as_iso(value: Any) -> str | None:
    """A ClickHouse timestamp as RFC 3339, or None if there isn't one.

    The MCP server hands DateTime back as `2026-08-27 15:10:00+03:00`. The
    finding contract asks for `date-time`, so the space becomes a `T` here
    rather than in three call sites.
    """
    if value in (None, "", 0):
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    text = str(value).strip()
    if not text or text.startswith("1970-01-01"):
        return None
    return text.replace(" ", "T", 1)


def scene_line(
    scene: str, line: int, quote: str | None, revision_id: str = ""
) -> dict | None:
    """A scene_line evidence object, or None when the quote is not there."""
    text = (quote or "").strip()
    if not scene or int(line or 0) < 1 or not text:
        return None
    evidence = {
        "type": "scene_line",
        "scene": str(scene),
        "line": int(line),
        "quote": text[:MAX_QUOTE],
    }
    if revision_id:
        evidence["revision_id"] = revision_id
    return evidence


def decision(row: dict) -> dict | None:
    """A decision evidence object. Dropped if it carries no reason.

    schema.sql: `reason` is "the WHY. Mandatory." A decision with an empty reason
    cannot support a claim about why something was chosen, so it is not cited.
    """
    decision_id = str(row.get("decision_id", "") or "")
    reason = str(row.get("reason", "") or "").strip()
    if not decision_id or not reason:
        return None
    evidence = {"type": "decision", "decision_id": decision_id, "reason": reason}
    decided_at = as_iso(row.get("decided_at"))
    if decided_at:
        evidence["decided_at"] = decided_at
    decided_by = str(row.get("decided_by", "") or "").strip()
    if decided_by:
        evidence["decided_by"] = decided_by
    return evidence


def commitment(row: dict) -> dict | None:
    """A commitment evidence object: what has already been paid for."""
    entity_id = str(row.get("entity_id", "") or "")
    state = str(row.get("commitment_state", row.get("state", "")) or "")
    if not entity_id or not state:
        return None
    evidence = {"type": "commitment", "entity_id": entity_id, "state": state}
    name = str(row.get("entity_name", "") or "").strip()
    if name:
        evidence["entity_name"] = name
    committed_at = as_iso(row.get("committed_at"))
    if committed_at:
        evidence["committed_at"] = committed_at
    return evidence


def dedupe(objects: Iterable[dict | None]) -> list[dict]:
    """Drop Nones and repeats, keeping the order things were cited in."""
    seen: set[tuple] = set()
    kept: list[dict] = []
    for obj in objects:
        if not obj:
            continue
        key = tuple(sorted((k, str(v)) for k, v in obj.items()))
        if key in seen:
            continue
        seen.add(key)
        kept.append(obj)
    return kept


@dataclass(frozen=True)
class DecisionChoice:
    """The one logged decision that best explains a change to a set of entities.

    Best means: the most overlap with the entities the changed fact is about,
    and among equals, the most recent. Overlap first is what keeps a finding
    about *who knows what* from citing the decision about the prop stock — both
    touch the letter, only one is about the move.
    """

    row: dict
    overlap: int


def best_decision(rows: list[dict], entity_ids: Iterable[str]) -> dict | None:
    """The decision to cite for a fact about `entity_ids`, or None."""
    wanted = set(entity_ids)
    if not wanted:
        return None
    candidates: list[DecisionChoice] = []
    for row in rows:
        overlap = len(wanted & set(row.get("entity_ids") or []))
        if overlap:
            candidates.append(DecisionChoice(row, overlap))
    if not candidates:
        return None
    candidates.sort(
        key=lambda c: (c.overlap, str(c.row.get("decided_at", ""))), reverse=True
    )
    return candidates[0].row


# --- carrying a citation across a revision ---------------------------------
def carry_forward(
    edges: list,
    current_facts: dict[str, dict],
    pages: "ScriptLines | None",
    prior_revision_id: str,
) -> tuple[list, list[str]]:
    """Re-point prior-revision dependency edges at the current pages.

    WHY THIS EXISTS, because it is not obvious and it is the crux of the catch.

    The Extractor visits scenes in script order, so a scene can only depend on a
    fact established BEFORE it. When the goldenrod revision moves the letter
    reveal from scene 18 to scene 31, the edges 22 -> letter and 24 -> letter
    simply cannot be expressed in the new graph — those scenes now come first.
    The dependency does not disappear from the pages, only from what the graph
    can say. That silence is the break.

    So the edges are read out of the PRIOR revision, where they could be
    expressed, and pointed at the current revision here:

      * the fact side is replaced with the fact as it now stands — the scene it
        moved to, and the line it is on there;
      * the scene side keeps the quote, which is still verbatim in the pages,
        and its line number is re-found in the current file, because a revision
        re-flows every line below the change;
      * if the line cannot be re-found uniquely, the citation keeps the prior
        revision's line number and is tagged with that revision id. It is still
        a real line in a real file — it is just labelled honestly.

    Callers must have already established that the scene is byte-identical
    between the two revisions. An edge from a scene the writer edited is not
    carried forward at all: they may have fixed it, and flagging a scene someone
    has just rewritten is exactly the false positive that ends adoption.
    """
    from dataclasses import replace

    carried, notes = [], []
    for edge in edges:
        fact = current_facts.get(edge.fact_match_key)
        if fact is None:
            continue
        line, cited_revision = edge.evidence_line, prior_revision_id
        if pages is None:
            notes.append(
                f"scene {edge.scene}: no pages supplied, so the citation keeps its "
                f"{prior_revision_id} line number"
            )
        else:
            found = pages.find_unique(edge.evidence_quote)
            if found:
                line, cited_revision = found, ""
            else:
                notes.append(
                    f"scene {edge.scene}: quote could not be placed in the current "
                    f"pages, cited against {prior_revision_id} instead"
                )
        carried.append(
            replace(
                edge,
                evidence_line=line,
                cited_revision=cited_revision,
                fact_key=str(fact.get("fact_key", edge.fact_key)),
                fact_kind=str(fact.get("kind", edge.fact_kind)),
                statement=str(fact.get("statement", edge.statement)),
                established_in_scene=str(fact.get("established_in_scene", "")),
                source_line=int(fact.get("source_line") or 0),
                entity_ids=tuple(fact.get("fact_entity_ids") or edge.entity_ids),
                carried=True,
            )
        )
    return carried, notes
