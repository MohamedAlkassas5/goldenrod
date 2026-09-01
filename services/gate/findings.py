"""Breaks become findings: evidence attached, ranked by what has been paid for.

Three things happen here, and the order matters.

1.  EVIDENCE IS ATTACHED. Every claim gets the line that makes it, the line that
    caused it, the logged decision that explains it and the commitment that
    prices it — each copied out of something that already exists (evidence.py).

2.  THE DROP RULE BITES. CLAUDE.md rule 1: no finding without evidence, enforced
    in code and not by a prompt. A finding with an empty evidence array is
    dropped, and so is one the contract rejects for any other reason. What was
    dropped is reported, never silently swallowed.

3.  RANKING. CLAUDE.md rule 3: findings are ranked by commitment state, not by
    count and not by a severity the model chose. The rank is `commitmentRank`,
    computed by ClickHouse — this module never maps a state to a number, because
    there is exactly one ranking implementation and it is the UDF in schema.sql.

SEVERITY IS DERIVED, NOT ASSIGNED
---------------------------------
Severity is a function of the commitment rank the database returned:

    rank 0–1  (shot, built, cast)                    -> high
    rank 2–4  (permitted, scouted, sourced, planned)  -> medium
    rank 5    (nothing committed)                     -> low

An element that has been shot cannot be changed without a company day; one that
is only on the one-liner can be changed with an email. That is the whole
severity model, it is inspectable, and no model chose it.

DEPARTMENTS ARE DERIVED HERE TOO
--------------------------------
Each finding carries the crew departments that own the elements it is about, so
role-scoped access is a property of the finding rather than something the API
re-derives per request. The mapping is in services/common/access.py; the Gate
does not know or care who is looking.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Iterable

from jsonschema import Draft202012Validator

from services.common.access import departments_for
from services.gate import evidence as ev
from services.gate.evidence import ScriptLines
from services.gate.rules import Break
from services.gate.scene_order import SceneOrder

REPO_ROOT = Path(__file__).resolve().parents[2]
FINDING_SCHEMA = REPO_ROOT / "contracts" / "finding.schema.json"

# Same fixed namespace convention as services/loader/seed.py: a stable handle
# maps to the same UUID on every machine, forever. That is what lets a finding
# keep its identity across runs, which is what makes "mark it intentional"
# silence it tomorrow as well as today.
GATE_NAMESPACE = uuid.UUID("3d1c9f42-7a58-5b6e-8f01-2c4d6e8a0b13")

# Boundaries of the severity bands, in terms of the rank commitmentRank returns.
HIGH_THROUGH_RANK = 1
MEDIUM_THROUGH_RANK = 4

# commitmentRank('') is 5, the same as 'none' — a scene with nothing logged
# against it ranks lowest-risk without a special case. Verified on ClickHouse 26.9.
UNCOMMITTED_RANK = 5
UNCOMMITTED_STATE = "none"


def _validator() -> Draft202012Validator:
    return Draft202012Validator(
        json.loads(FINDING_SCHEMA.read_text(encoding="utf-8"))
    )


def finding_id(production_id: str, scene: str, rule: str, match_key: str) -> str:
    """Stable id for one break. Same break, same id, every run.

    Deliberately not a function of the shoot date or the run: the same break
    surfacing on a later call sheet is the same break, and a deviation marked
    intentional yesterday must still be silent tomorrow.
    """
    return str(
        uuid.uuid5(GATE_NAMESPACE, f"{production_id}|{scene}|{rule}|{match_key}")
    )


def dismissal_id(production_id: str, finding: str) -> str:
    """The ledger id of the decision that marks one finding intentional.

    Derived from the finding id rather than stored beside it, so suppression
    needs no extra table and no string parsing: the Gate computes the id it
    would have written and asks whether the ledger already holds it.
    """
    return str(uuid.uuid5(GATE_NAMESPACE, f"dismissal|{production_id}|{finding}"))


def severity_for(commitment_rank: int) -> str:
    """Severity from the rank ClickHouse computed. See the module docstring."""
    rank = int(commitment_rank)
    if rank <= HIGH_THROUGH_RANK:
        return "high"
    if rank <= MEDIUM_THROUGH_RANK:
        return "medium"
    return "low"


# --- commitment state per scene --------------------------------------------
def _scene_commitment(
    scene: str, commitments: list[dict], prefer: Iterable[str]
) -> dict | None:
    """The most-committed element in a scene, preferring what the break is about.

    `scene_commitments` comes back ordered by commitment_rank ascending, so the
    first row for a scene is already the highest-risk one. Among elements at
    that same rank, an entity the changed fact is actually about wins, then a
    character over a prop or a room — the one a coordinator would name.
    """
    rows = [r for r in commitments if str(r.get("scene_id")) == scene]
    if not rows:
        return None
    top = min(int(r["commitment_rank"]) for r in rows)
    tied = [r for r in rows if int(r["commitment_rank"]) == top]
    wanted = set(prefer)
    tied.sort(
        key=lambda r: (
            str(r.get("entity_id")) not in wanted,
            str(r.get("entity_type")) != "character",
            str(r.get("entity_id")),
        )
    )
    return tied[0]


# --- assembly --------------------------------------------------------------
def build_findings(
    breaks: list[Break],
    *,
    production_id: str,
    shoot_date: str,
    revision_id: str,
    commitments: list[dict],
    decisions: list[dict],
    order: SceneOrder,
    pages: ScriptLines | None = None,
    scheduled: Iterable[str] = (),
    entity_types: dict[str, str] | None = None,
) -> tuple[list[dict], list[str], list[dict]]:
    """Breaks in, ranked findings out.

    Returns `(findings, dropped, dismissed)`:
      * findings  contract-valid, ranked, ready to render
      * dropped   one line per finding refused, with the reason
      * dismissed findings silenced by a logged intentional deviation, kept so
                  the run can show that the silence was earned rather than lucky
    """
    validator = _validator()
    scheduled_scenes = set(scheduled)
    types = entity_types or {}
    silenced = {
        str(d.get("decision_id"))
        for d in decisions
        if int(d.get("intentional_deviation") or 0) == 1
    }

    built: list[tuple[int, bool, tuple, dict]] = []
    dropped: list[str] = []
    dismissed: list[dict] = []

    for brk in breaks:
        identifier = finding_id(production_id, brk.scene, brk.rule, brk.fact.match_key)
        state_row = _scene_commitment(brk.scene, commitments, brk.entity_ids)
        rank = int(state_row["commitment_rank"]) if state_row else UNCOMMITTED_RANK
        state = str(state_row["commitment_state"]) if state_row else UNCOMMITTED_STATE

        chosen = ev.best_decision(
            [d for d in decisions if str(d.get("scene_id")) == brk.establishing_scene],
            brk.entity_ids,
        )
        # A deviation already accepted elsewhere on the same fact. Cited because
        # it changes what a coordinator should do: if the production office has
        # decided the letter now reads in 31 and signed off scene 24 on that
        # basis, the right question about scene 22 is a pickup, not a rewrite.
        related = ev.best_decision(
            [
                d
                for d in decisions
                if int(d.get("intentional_deviation") or 0) == 1
                and str(d.get("scene_id")) != brk.scene
            ],
            brk.entity_ids,
        )

        objects: list[dict | None] = []
        for edge in brk.edges:
            # `cited_revision` is set only when a carried-forward citation could
            # not be placed in the current pages, so the line number belongs to
            # the prior revision and the evidence says so.
            objects.append(
                ev.scene_line(
                    brk.scene,
                    edge.evidence_line,
                    edge.evidence_quote,
                    edge.cited_revision or revision_id,
                )
            )
        # The line that CAUSED the break, read back out of the current pages.
        # Only when the fact still exists in this revision: for a removed fact
        # the line lives in the prior revision's pages, and citing it against
        # this revision's line numbers would point at whatever now sits there.
        if pages is not None and brk.fact.now_scene:
            objects.append(
                ev.scene_line(
                    brk.fact.now_scene,
                    brk.edges[0].source_line,
                    pages.quote(brk.edges[0].source_line),
                    revision_id,
                )
            )
        objects.append(ev.decision(chosen or {}))
        objects.append(ev.decision(related or {}))
        objects.append(ev.commitment(state_row or {}))

        finding: dict[str, Any] = {
            "finding_id": identifier,
            "scene": brk.scene,
            "shoot_date": shoot_date,
            "severity": severity_for(rank),
            "commitment_state": state,
            "kind": brk.kind,
            "claim": brk.claim,
            # Which departments this belongs to, from the types of the elements
            # the break is about. Computed here, once, rather than at read time:
            # it is a property of the finding, and the API must never have to
            # re-derive who owns something in order to decide who may see it.
            "departments": departments_for(
                types.get(e, "") for e in brk.entity_ids
            ),
            "evidence": ev.dedupe(objects),
            "suggested_action": _action_for(brk, state, related),
        }

        # CLAUDE.md rule 1, in code. A finding that cannot cite does not ship.
        if not finding["evidence"]:
            dropped.append(f"{brk.scene}/{brk.rule}: no evidence survived, dropped")
            continue
        errors = sorted(validator.iter_errors(finding), key=lambda e: list(e.path))
        if errors:
            dropped.append(
                f"{brk.scene}/{brk.rule}: contract rejected it — {errors[0].message}"
            )
            continue

        marker = dismissal_id(production_id, identifier)
        if marker in silenced:
            note = next(d for d in decisions if str(d.get("decision_id")) == marker)
            finding["dismissed"] = {
                "intentional": True,
                "reason": str(note.get("deviation_reason") or note.get("reason") or ""),
                "marked_by": str(note.get("decided_by") or ""),
                "marked_at": ev.as_iso(note.get("decided_at")) or "",
            }
            dismissed.append(finding)
            continue

        built.append(
            (rank, brk.scene not in scheduled_scenes, order.position(brk.scene), finding)
        )

    # CLAUDE.md rule 3: commitment state first, always. A scene that shoots
    # tomorrow breaks a tie, and script order breaks what is left, so two runs
    # over the same data always render in the same order.
    built.sort(key=lambda row: (row[0], row[1], row[2]))
    return [row[3] for row in built], dropped, dismissed


def _action_for(brk: Break, state: str, related: dict | None = None) -> str:
    """The suggested action, adjusted for what has already been spent.

    A scene that is already in the can cannot be fixed by cutting a line; the
    only options are to accept it or to schedule a pickup, and saying so is the
    difference between advice a 1st AD can act on and advice they cannot.

    Once the same deviation has been accepted somewhere else, "confirm
    intentional" is no longer the open question it was — somebody already did,
    and the wording says so rather than asking again.
    """
    if related:
        scene = str(related.get("scene_id", ""))
        settled = f"Already accepted as intentional on scene {scene}. "
        if state == "shot":
            return (settled + f"Raise {brk.scene} as a pickup, or accept it too.")[:240]
        return (settled + f"Match it in {brk.scene}, or accept it too.")[:240]
    if state == "shot":
        return (
            f"Already shot. Confirm intentional, or raise {brk.scene} as a pickup."
        )[:240]
    return brk.action[:240]
