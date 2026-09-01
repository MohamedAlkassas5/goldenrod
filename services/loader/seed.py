"""Seed the ledger, commitment state and the access roster, through the MCP server.

    python -m services.loader.seed                    # load data/seed/
    python -m services.loader.seed --dry-run          # validate only
    python -m services.loader.seed --check            # report what is already loaded

Determinism and repeat safety
-----------------------------
The tables need different treatment, because they are deliberately different
engines:

`commitments` is a ReplacingMergeTree keyed on (production_id, entity_id,
scene_id). Re-seeding writes the same sorting key with a newer `updated_at`, so
the row is replaced. Idempotent for free.

`decisions` is a plain MergeTree, on purpose: a ledger is append-only and
rewriting history would defeat the point of storing WHY. It therefore does NOT
deduplicate, and a naive re-seed would double every row. So each seed entry
carries a stable `seed_key`, `decision_id` is derived from it as a UUIDv5, and
this loader reads the ids already present and inserts only what is missing.

`access_grants` is a ReplacingMergeTree keyed on (production_id, subject), so a
re-seed replaces a person's grant instead of adding a second one. The newest
grant is the one in force, which is what a roster needs, and revoking somebody is
an edit to the seed rather than a DELETE.

That keeps repeat runs safe without any UPDATE or DELETE — which is required,
not merely tidy: the MCP server runs with CLICKHOUSE_ALLOW_DROP=false and
refuses destructive statements.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from services.common.access import ROLES, get_role
from services.common.mcp_client import ClickHouseMCP, MCPError
from services.common.sql import insert, lit

REPO_ROOT = Path(__file__).resolve().parents[2]
SEED_SCHEMA = REPO_ROOT / "contracts" / "seed.schema.json"
SEED_DIR = REPO_ROOT / "data" / "seed"

# Fixed namespace so a seed_key maps to the same UUID on every machine, forever.
SEED_NAMESPACE = uuid.UUID("6f0d5a1e-0b26-5f7a-9c4d-8e1f2a3b4c5d")

DECISION_COLUMNS = [
    "decision_id", "production_id", "revision_id", "scene_id", "entity_ids",
    "decision_type", "selected_option", "alternatives", "reason", "cause_tag",
    "decided_by", "decided_at", "status", "intentional_deviation", "deviation_reason",
]

COMMITMENT_COLUMNS = [
    "commitment_id", "production_id", "entity_id", "scene_id", "state",
    "cost_band", "committed_at", "updated_at", "notes",
]

ACCESS_COLUMNS = [
    "production_id", "subject", "display_name", "role", "granted_by",
    "granted_at", "updated_at", "notes",
]


class SeedError(RuntimeError):
    """The seed was refused. Nothing was written."""


def seed_uuid(production_id: str, kind: str, seed_key: str) -> str:
    """Stable UUID for a seed row. Same inputs, same id, every run."""
    return str(uuid.uuid5(SEED_NAMESPACE, f"{production_id}|{kind}|{seed_key}"))


# --- validate --------------------------------------------------------------
def validate_seed(seed: dict[str, Any]) -> None:
    schema = json.loads(SEED_SCHEMA.read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(schema).iter_errors(seed), key=lambda e: list(e.path)
    )
    if errors:
        detail = "\n".join(
            f"  {'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}"
            for e in errors[:25]
        )
        raise SeedError(
            f"seed failed contracts/seed.schema.json ({len(errors)} errors):\n{detail}"
        )

    for kind, key in (("decisions", "seed_key"), ("commitments", "seed_key")):
        keys = [row[key] for row in seed.get(kind, [])]
        duplicates = sorted({k for k in keys if keys.count(k) > 1})
        if duplicates:
            raise SeedError(
                f"duplicate seed_key in {kind}: {duplicates}. seed_key must be unique "
                f"— it is what makes re-seeding idempotent."
            )

    # The access roster is keyed on the subject, not on a seed_key: a person is
    # already unique. Two grants for one subject is not a duplicate row to
    # collapse, it is an ambiguity about what somebody is entitled to, and the
    # only safe reading of an ambiguous grant is to refuse the whole seed.
    subjects = [str(row["subject"]).strip().lower() for row in seed.get("access", [])]
    clashes = sorted({s for s in subjects if subjects.count(s) > 1})
    if clashes:
        raise SeedError(
            f"the same subject is granted more than one role: {clashes}. "
            f"One subject, one role per production."
        )

    # The contract's enum and services/common/access.py can drift apart; a role
    # that exists in one and not the other would grant nothing and say nothing.
    unknown = sorted(
        {
            str(row["role"])
            for row in seed.get("access", [])
            if get_role(str(row["role"])) is None
        }
    )
    if unknown:
        raise SeedError(
            f"unknown role(s) {unknown}. Roles are declared in "
            f"services/common/access.py: {sorted(ROLES)}"
        )


# --- resolve ---------------------------------------------------------------
def resolve_seed(seed: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Seed entries -> row dicts, with deterministic ids filled in."""
    production_id = seed["production_id"]

    decisions = [
        {
            "decision_id": seed_uuid(production_id, "decision", d["seed_key"]),
            "production_id": production_id,
            "revision_id": d.get("revision_id", ""),
            "scene_id": d["scene_id"],
            "entity_ids": sorted(set(d.get("entity_ids") or [])),
            "decision_type": d["decision_type"],
            "selected_option": d["selected_option"],
            "alternatives": d.get("alternatives") or [],
            "reason": d["reason"],
            "cause_tag": d["cause_tag"],
            "decided_by": d["decided_by"],
            "decided_at": d["decided_at"],
            "status": d.get("status", "active"),
            "intentional_deviation": int(d.get("intentional_deviation", 0)),
            "deviation_reason": d.get("deviation_reason", ""),
        }
        for d in seed.get("decisions", [])
    ]

    commitments = [
        {
            "commitment_id": seed_uuid(production_id, "commitment", c["seed_key"]),
            "production_id": production_id,
            "entity_id": c["entity_id"],
            "scene_id": c["scene_id"],
            "state": c["state"],
            "cost_band": c["cost_band"],
            "committed_at": c["committed_at"],
            "notes": c.get("notes", ""),
        }
        for c in seed.get("commitments", [])
    ]

    access = [
        {
            "production_id": production_id,
            # Lower-cased once here so the lookup at request time is an exact
            # match rather than a per-row function call over the roster.
            "subject": str(a["subject"]).strip().lower(),
            "display_name": a.get("display_name", ""),
            "role": a["role"],
            "granted_by": a.get("granted_by", ""),
            "granted_at": a.get("granted_at") or datetime(2026, 1, 1),
            "notes": a.get("notes", ""),
        }
        for a in seed.get("access", [])
    ]

    return {"decisions": decisions, "commitments": commitments, "access": access}


# --- write -----------------------------------------------------------------
def seed_ledger(
    seed: dict[str, Any], ch: ClickHouseMCP, now: datetime | None = None
) -> dict[str, Any]:
    """Validate, resolve and write. Returns what was inserted vs skipped."""
    validate_seed(seed)
    rows = resolve_seed(seed)
    production_id = seed["production_id"]
    scope = f"production_id = {lit(production_id)}"

    # decisions: append-only, so insert only ids not already present
    existing = {
        r["decision_id"]
        for r in ch.rows(f"SELECT DISTINCT decision_id FROM decisions WHERE {scope}")
    }
    new_decisions = [d for d in rows["decisions"] if d["decision_id"] not in existing]
    if new_decisions:
        ch.run_query(insert("decisions", DECISION_COLUMNS, new_decisions))

    # commitments: ReplacingMergeTree collapses on (production_id, entity_id,
    # scene_id), so writing every row again simply replaces it. updated_at is the
    # version column, so it must advance for the new row to win.
    stamp = now or datetime.now(timezone.utc).replace(tzinfo=None)
    commitment_rows = [{**c, "updated_at": stamp} for c in rows["commitments"]]
    sql = insert("commitments", COMMITMENT_COLUMNS, commitment_rows)
    if sql:
        ch.run_query(sql)

    # access_grants: ReplacingMergeTree on (production_id, subject), so a
    # re-seed replaces a person's grant rather than adding a second one. That is
    # the behaviour a roster needs — the newest grant is the one in force — and
    # it means revoking someone is a seed edit, not a DELETE the MCP server
    # would refuse anyway.
    access_rows = [{**a, "updated_at": stamp} for a in rows["access"]]
    sql = insert("access_grants", ACCESS_COLUMNS, access_rows)
    if sql:
        ch.run_query(sql)

    return {
        "production_id": production_id,
        "decisions_inserted": len(new_decisions),
        "decisions_skipped": len(rows["decisions"]) - len(new_decisions),
        "commitments_written": len(rows["commitments"]),
        "access_written": len(access_rows),
    }


def load_seed_files(directory: Path | None = None) -> dict[str, Any]:
    """Merge data/seed/*.seed.json into one seed document."""
    directory = directory or SEED_DIR
    merged: dict[str, Any] = {}
    for path in sorted(directory.glob("*.seed.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        production_id = doc["production_id"]
        if merged and merged["production_id"] != production_id:
            raise SeedError(
                f"{path.name} is for production {production_id!r} but the other seed "
                f"files are for {merged['production_id']!r}."
            )
        merged.setdefault("production_id", production_id)
        for key in ("decisions", "commitments", "access"):
            if key in doc:
                merged.setdefault(key, []).extend(doc[key])
    if not merged:
        raise SeedError(f"no *.seed.json files in {directory}")
    return merged


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m services.loader.seed")
    parser.add_argument("--dir", type=Path, default=SEED_DIR)
    parser.add_argument("--dry-run", action="store_true", help="validate, write nothing")
    parser.add_argument("--check", action="store_true", help="report what is loaded")
    args = parser.parse_args(argv)

    try:
        seed = load_seed_files(args.dir)
        validate_seed(seed)
        rows = resolve_seed(seed)

        if args.dry_run:
            print(f"valid: {args.dir}")
            print(f"  decisions    {len(rows['decisions']):>3}")
            print(f"  commitments  {len(rows['commitments']):>3}")
            print(f"  access       {len(rows['access']):>3}")
            print("\nnothing written (--dry-run)")
            return 0

        with ClickHouseMCP() as ch:
            if args.check:
                production = lit(seed["production_id"])
                for table, extra in (("decisions", "status"), ("commitments", "state")):
                    counts = ch.rows(
                        f"SELECT {extra} AS k, count() AS n FROM {table} "
                        f"{'FINAL ' if table == 'commitments' else ''}"
                        f"WHERE production_id = {production} GROUP BY k ORDER BY k"
                    )
                    print(f"  {table}:")
                    for row in counts:
                        print(f"    {row['k'] or '(empty)':<12} {row['n']}")
                return 0

            result = seed_ledger(seed, ch)

        print(f"seeded {result['production_id']}")
        print(f"  decisions    inserted {result['decisions_inserted']}, "
              f"skipped {result['decisions_skipped']} (already present)")
        print(f"  commitments  written  {result['commitments_written']} (replaced in place)")
        print(f"  access       written  {result['access_written']} (replaced in place)")
        return 0

    except (SeedError, MCPError) as exc:
        print(f"\nSEED FAILED\n{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
