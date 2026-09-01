"""Writing back: a run's findings, and a human marking one intentional.

Both writes go through the MCP server, like every other statement in this
codebase.

MARKING A FINDING INTENTIONAL WRITES TO THE LEDGER, NOT TO THE FINDING
---------------------------------------------------------------------
SPEC §8: "Marking a finding intentional silences it permanently and writes a row
to the ledger." It writes ONLY to the ledger. `decisions` is a plain MergeTree
because a ledger is append-only, `findings` is one for the same reason, and the
MCP server runs with CLICKHOUSE_ALLOW_DROP=false — there is no UPDATE anywhere
in this project and there is not meant to be.

So suppression is computed at run time instead of stored: the dismissal's
`decision_id` is a deterministic function of the finding id (findings.py), and
the next run asks the ledger whether that id is present. Nothing has to be kept
in sync, and the reason the coordinator gave becomes evidence on every later run
— which is the 2:00–2:30 beat of the demo, and the reason the re-run genuinely
differs rather than appearing to.

A dismissal is a real decision with a real reason, so it also shows up in
`commitment_ranking` alongside every other decision touching those entities.
That is deliberate: a related finding on the same fact picks it up as evidence
and reads differently afterwards.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from services.common.mcp_client import ClickHouseMCP
from services.common.sql import insert, lit
from services.gate.findings import dismissal_id

FINDING_COLUMNS = [
    "finding_id", "production_id", "run_id", "scene_id", "shoot_date", "kind",
    "severity", "commitment_state", "claim", "evidence_json", "dismissed",
    "dismiss_reason", "created_at",
]

DECISION_COLUMNS = [
    "decision_id", "production_id", "revision_id", "scene_id", "entity_ids",
    "decision_type", "selected_option", "alternatives", "reason", "cause_tag",
    "decided_by", "decided_at", "status", "intentional_deviation", "deviation_reason",
]

# A deviation the production office chose on purpose. `cause_tag` is one tap and
# four options (schema.sql); accepting a break as intended is a judgement call,
# which is `taste`. Overridable, because a network note that forced the deviation
# is `external_note` and the distinction is the reason the column exists.
DEFAULT_CAUSE_TAG = "taste"


def persist_findings(ch: ClickHouseMCP, run: Any) -> int:
    """Write one run's findings, including the ones a deviation silenced.

    The silenced ones are written with `dismissed = 1` on purpose: a run that
    found something and said nothing about it, because a human had already
    accepted it, is a different event from a run that found nothing, and the
    findings table is where that distinction has to survive.
    """
    rows: list[dict[str, Any]] = []
    for finding in list(run.findings) + list(run.dismissed):
        note = finding.get("dismissed") or {}
        rows.append(
            {
                "finding_id": finding["finding_id"],
                "production_id": run.production_id,
                "run_id": run.run_id,
                "scene_id": finding["scene"],
                "shoot_date": finding["shoot_date"],
                "kind": finding.get("kind", ""),
                "severity": finding["severity"],
                "commitment_state": finding["commitment_state"],
                "claim": finding["claim"],
                "evidence_json": json.dumps(finding["evidence"], ensure_ascii=False),
                "dismissed": 1 if note else 0,
                "dismiss_reason": str(note.get("reason", "")),
                "created_at": run.started_at,
            }
        )
    sql = insert("findings", FINDING_COLUMNS, rows)
    if sql:
        ch.run_query(sql)
    return len(rows)


def mark_intentional(
    ch: ClickHouseMCP,
    production_id: str,
    finding: dict,
    reason: str,
    marked_by: str,
    *,
    revision_id: str = "",
    cause_tag: str = DEFAULT_CAUSE_TAG,
    now: datetime | None = None,
) -> dict:
    """Record that a finding is a deliberate deviation. Silences it from now on.

    Idempotent: the decision id is derived from the finding id, so marking the
    same finding twice writes the same row rather than a second one.
    """
    reason = (reason or "").strip()
    if not reason:
        raise ValueError(
            "an intentional deviation needs a reason. schema.sql: `reason` is "
            "'the WHY. Mandatory.' — a silenced finding with no reason is a "
            "finding nobody can audit."
        )
    marked_by = (marked_by or "").strip()
    if not marked_by:
        raise ValueError(
            "every ledger write is attributed. An unsigned dismissal of an "
            "unreleased-script finding is exactly what governance is for."
        )

    decision_id = dismissal_id(production_id, finding["finding_id"])
    stamp = (now or datetime.now(timezone.utc)).replace(tzinfo=None, microsecond=0)
    entity_ids = sorted(
        {
            e["entity_id"]
            for e in finding.get("evidence", [])
            if e.get("type") == "commitment" and e.get("entity_id")
        }
    )

    existing = ch.rows(
        f"SELECT decision_id FROM decisions WHERE production_id = {lit(production_id)} "
        f"AND decision_id = toUUID({lit(decision_id)})"
    )
    if not existing:
        ch.run_query(
            insert(
                "decisions",
                DECISION_COLUMNS,
                [
                    {
                        "decision_id": decision_id,
                        "production_id": production_id,
                        "revision_id": revision_id,
                        "scene_id": finding["scene"],
                        "entity_ids": entity_ids,
                        "decision_type": "story",
                        "selected_option": f"Accepted as intentional: {finding['claim']}"[:400],
                        "alternatives": [],
                        "reason": reason,
                        "cause_tag": cause_tag,
                        "decided_by": marked_by,
                        "decided_at": stamp,
                        "status": "active",
                        "intentional_deviation": 1,
                        "deviation_reason": reason,
                    }
                ],
            )
        )

    return {
        "decision_id": decision_id,
        "finding_id": finding["finding_id"],
        "scene": finding["scene"],
        "already_marked": bool(existing),
        "marked_by": marked_by,
        "marked_at": stamp.isoformat(),
    }
