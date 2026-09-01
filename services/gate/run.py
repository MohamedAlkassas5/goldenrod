"""The Gate: the orchestrator.

    TRIGGER: call sheet issued
       ├─ 1. PARSE DAY         scene numbers scheduled for the shoot date
       ├─ 2. DRAFT DIFF        fact-level change list, current vs prior revision
       ├─ 3. TRAVERSE          dependent scenes + entities, per changed fact
       ├─ 4. LEDGER QUERY      decisions touching them, with the reason at the time
       ├─ 5. COMMITMENT LOOKUP what has already been paid for
       └─ 6. RANK + CITE       order by commitment cost, evidence on every claim

Deterministic and multi-step, and every stage produces structured output the
next one consumes. That is the thing that separates this from a chat wrapper —
and SPEC §3 asks for the intermediate state to be visible, so each step records
what it did, how long it took and the SQL it sent. `GateRun.steps` is what the UI
renders as the pipeline runs, and step 4's SQL is the frame that carries most of
the Technological Implementation score.

The Gate calls tools; it does not free-associate. There is no model call in this
file. Everything it asserts came out of the graph the Extractor built, the
ledger the production office wrote, or the pages themselves.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from jsonschema import Draft202012Validator

from services.common.mcp_client import ClickHouseMCP
from services.gate.evidence import ScriptLines, carry_forward
from services.gate.findings import build_findings
from services.gate.reader import GateReader
from services.gate.rules import ChangedFact, DependencyEdge, detect
from services.gate.scene_order import SceneOrder

REPO_ROOT = Path(__file__).resolve().parents[2]
CALL_SHEET_SCHEMA = REPO_ROOT / "contracts" / "call-sheet.schema.json"

# Rows carried back with each step for the interface to display. Enough to show
# the query did real work; not so many that a run becomes a data export.
SAMPLE_ROWS = 12


class GateError(RuntimeError):
    """The check could not run. Nothing was written."""


@dataclass
class Step:
    """One pipeline stage, as it happened. Rendered by the UI in order."""

    name: str
    label: str
    rows: int = 0
    ms: int = 0
    detail: str = ""
    sql: str = ""
    # The first few rows the query actually returned. The demo puts the query
    # and its result side by side; a query with no result beside it proves
    # nothing ran.
    sample: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "rows": self.rows,
            "ms": self.ms,
            "detail": self.detail,
            "sql": self.sql,
            "sample": self.sample,
        }


@dataclass
class GateRun:
    """One check: what was asked, what happened, and what came out."""

    run_id: str
    production_id: str
    shoot_date: str
    revision_id: str
    prior_revision_id: str
    scheduled_scenes: list[str]
    started_at: datetime
    steps: list[Step] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)
    # Findings refused for want of evidence, and citations that had to be
    # weakened. Both are printed. Nothing this check discards is discarded
    # quietly — that is how precision gets tuned (CLAUDE.md rule 6).
    dropped: list[str] = field(default_factory=list)
    dismissed: list[dict] = field(default_factory=list)
    sql: dict[str, str] = field(default_factory=dict)

    @property
    def ms(self) -> int:
        return sum(step.ms for step in self.steps)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "production_id": self.production_id,
            "shoot_date": self.shoot_date,
            "revision_id": self.revision_id,
            "prior_revision_id": self.prior_revision_id,
            "scheduled_scenes": self.scheduled_scenes,
            "started_at": self.started_at.isoformat(),
            "ms": self.ms,
            "steps": [s.as_dict() for s in self.steps],
            "findings": self.findings,
            "dismissed": self.dismissed,
            "dropped": self.dropped,
        }

    def summary(self) -> str:
        lines = [
            f"{self.production_id} — {self.shoot_date} "
            f"({self.revision_id} vs {self.prior_revision_id})",
            f"  scheduled  {', '.join(self.scheduled_scenes) or '(none)'}",
        ]
        for step in self.steps:
            lines.append(
                f"  {step.name:<18} {step.rows:>4} rows  {step.ms:>5}ms  {step.detail}"
            )
        lines.append("")
        if not self.findings:
            lines.append("  nothing shoots tomorrow that this revision broke.")
        for finding in self.findings:
            lines.append(
                f"  [{finding['commitment_state']:<9}] scene {finding['scene']:<4} "
                f"{finding['severity']:<6} {finding['kind']}"
            )
            lines.append(f"      {finding['claim']}")
            for item in finding["evidence"]:
                lines.append(f"      · {_render_evidence(item)}")
            lines.append(f"      -> {finding['suggested_action']}")
        for finding in self.dismissed:
            note = finding.get("dismissed", {})
            lines.append(
                f"  [silenced ] scene {finding['scene']:<4} marked intentional by "
                f"{note.get('marked_by', '?')}: {note.get('reason', '')}"
            )
        for line in self.dropped:
            lines.append(f"  [dropped  ] {line}")
        return "\n".join(lines)


def _render_evidence(item: dict) -> str:
    if item["type"] == "scene_line":
        return f"scene {item['scene']} line {item['line']}: {item['quote']}"
    if item["type"] == "decision":
        return f"decision {item['decision_id'][:8]}: {item['reason']}"
    return (
        f"commitment {item.get('entity_name') or item['entity_id']}: {item['state']}"
    )


# --- step 1: parse day -----------------------------------------------------
def load_call_sheet(path: Path) -> dict:
    """Read and validate a call sheet against its contract."""
    try:
        call_sheet = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateError(f"could not read {path}: {exc}") from exc
    validate_call_sheet(call_sheet)
    return call_sheet


def validate_call_sheet(call_sheet: dict) -> None:
    schema = json.loads(CALL_SHEET_SCHEMA.read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(schema).iter_errors(call_sheet), key=lambda e: list(e.path)
    )
    if errors:
        detail = "\n".join(
            f"  {'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}"
            for e in errors[:10]
        )
        raise GateError(
            f"call sheet failed contracts/call-sheet.schema.json "
            f"({len(errors)} errors):\n{detail}"
        )


# --- the run ---------------------------------------------------------------
def run_gate(
    call_sheet: dict,
    ch: ClickHouseMCP,
    *,
    pages: ScriptLines | None = None,
    now: datetime | None = None,
    on_step: Callable[[Step], None] | None = None,
) -> GateRun:
    """Run the six-step check for one call sheet. Reads only; writes nothing.

    Persisting the result is a separate, explicit act — see
    services.gate.writeback.persist_findings. A check that cannot be run without
    changing the database is a check nobody will run twice.

    `on_step` is called as each stage completes, which is how the interface
    renders the pipeline running rather than animating a guess at it (SPEC §7,
    0:40–1:15). Same shape as the Extractor's `on_scene`.
    """
    validate_call_sheet(call_sheet)

    production_id = call_sheet["production_id"]
    revision_id = call_sheet["revision_id"]
    shoot_date = call_sheet["shoot_date"]
    scheduled = [str(s["scene_number"]) for s in call_sheet["scenes"]]

    reader = GateReader(ch, production_id)
    run = GateRun(
        run_id=str(uuid.uuid4()),
        production_id=production_id,
        shoot_date=shoot_date,
        revision_id=revision_id,
        prior_revision_id=call_sheet.get("prior_revision_id", ""),
        scheduled_scenes=scheduled,
        started_at=(now or datetime.now(timezone.utc)).replace(
            tzinfo=None, microsecond=0
        ),
    )

    def step(name: str, label: str) -> "_Timer":
        return _Timer(run, reader, name, label, on_step)

    # 1. PARSE DAY
    with step("parse_day", "Scenes scheduled for the shoot date") as s:
        s.rows = len(scheduled)
        s.detail = f"{len(scheduled)} scenes on the call sheet"

    # 2. DRAFT DIFF — fact level, not text
    with step("draft_diff", "Facts that changed since the prior revision") as s:
        if not run.prior_revision_id:
            run.prior_revision_id = reader.previous_revision(revision_id)
        if not run.prior_revision_id:
            raise GateError(
                f"no earlier revision of {production_id} on file to diff "
                f"{revision_id} against. Load the prior revision's graph first: "
                f"python -m services.loader <graph.json>"
            )

        # A check that has nothing to check must say so. Reporting "nothing
        # broke" because no graph was ever loaded is a false negative dressed
        # as reassurance, and this product is only worth running if silence
        # means silence.
        now_hashes = reader.scene_hashes(revision_id)
        was_hashes = reader.scene_hashes(run.prior_revision_id)
        for label, revision, scenes in (
            ("current", revision_id, now_hashes),
            ("prior", run.prior_revision_id, was_hashes),
        ):
            if not scenes:
                raise GateError(
                    f"no scenes on file for the {label} revision "
                    f"{revision!r} of {production_id!r}. Extract and load it "
                    f"before running the check:\n"
                    f"  python -m services.extractor <script.fountain> "
                    f"--production {production_id} --revision {revision} -o graph.json\n"
                    f"  python -m services.loader graph.json"
                )

        diff_rows = reader.draft_diff(run.prior_revision_id, revision_id)
        changed = [ChangedFact.from_row(r) for r in diff_rows]
        s.rows = len(changed)
        s.key = "draft_diff"
        s.detail = ", ".join(
            f"{c.change} {c.was_scene or '-'}->{c.now_scene or '-'}" for c in changed[:6]
        ) or "no facts changed"

    # 3. TRAVERSE — which untouched scenes lean on those facts
    #
    # Both revisions are traversed, and that is the whole trick. The Extractor
    # visits scenes in script order, so a scene can only depend on a fact
    # established before it: once the revision moves the letter reveal to scene
    # 31, the graph physically cannot say that 22 and 24 depend on it. Those
    # edges are read out of the prior revision, where they could be expressed,
    # and carried forward onto the current pages. See evidence.carry_forward.
    with step("traverse", "Scenes that depend on a changed fact") as s:
        match_keys = [c.match_key for c in changed]
        current_edges = [
            DependencyEdge.from_row(r)
            for r in reader.dependent_scenes(revision_id, match_keys)
        ]
        current_facts = reader.changed_facts(revision_id, match_keys)
        unchanged = {
            scene
            for scene, digest in now_hashes.items()
            if digest and was_hashes.get(scene) == digest
        }
        expressed = {(e.scene, e.fact_match_key) for e in current_edges}
        stale = [
            edge
            for edge in (
                DependencyEdge.from_row(r)
                for r in reader.dependent_scenes(run.prior_revision_id, match_keys)
            )
            if (edge.scene, edge.fact_match_key) not in expressed
            and edge.scene in unchanged
        ]
        carried, notes = carry_forward(
            stale, current_facts, pages, run.prior_revision_id
        )
        run.dropped.extend(notes)

        order = SceneOrder.of(
            list(now_hashes)
            + scheduled
            + [c.now_scene for c in changed if c.now_scene]
        )
        breaks = detect(
            changed,
            current_edges + carried,
            order,
            current_scenes=frozenset(now_hashes),
        )
        s.rows = len(current_edges) + len(carried)
        s.key = f"dependent_scenes:{run.prior_revision_id}"
        s.detail = (
            f"{len(current_edges)} edges in this revision, {len(carried)} carried "
            f"forward from {run.prior_revision_id} -> "
            f"{len(breaks)} break{'' if len(breaks) == 1 else 's'} in "
            f"{', '.join(order.sorted({b.scene for b in breaks})) or 'no scene'}"
        )

    entity_ids = sorted({e for b in breaks for e in b.entity_ids})
    scenes_of_interest = sorted({b.scene for b in breaks} | set(scheduled))

    # 4. LEDGER QUERY — decisions, aggregated, with the reason given at the time
    with step("ledger", "Decision history for the affected elements") as s:
        ranking = reader.commitment_ranking(entity_ids)
        decisions = reader.active_decisions()
        s.rows = len(ranking)
        s.key = "commitment_ranking"
        s.detail = (
            f"{len(decisions)} active decisions, "
            f"{len(ranking)} element/scene pairs with a logged decision"
        )

    # 5. COMMITMENT LOOKUP — what has already been paid for
    with step("commitment", "Commitment state for every affected scene") as s:
        commitments = reader.scene_commitments(scenes_of_interest)
        s.rows = len(commitments)
        s.key = "scene_commitments"
        s.detail = ", ".join(
            sorted({str(r["commitment_state"]) for r in commitments})
        ) or "nothing committed"

    # 6. RANK + CITE
    with step("rank", "Ranked by what has already been paid for") as s:
        run.findings, run.dropped, run.dismissed = build_findings(
            breaks,
            entity_types=reader.entity_types(entity_ids),
            production_id=production_id,
            shoot_date=shoot_date,
            revision_id=revision_id,
            commitments=commitments,
            decisions=decisions,
            order=order,
            pages=pages,
            scheduled=scheduled,
        )
        s.rows = len(run.findings)
        s.detail = (
            f"{len(run.findings)} findings, {len(run.dismissed)} silenced by a "
            f"logged deviation, {len(run.dropped)} dropped for want of evidence"
        )

    run.sql = dict(reader.sql)
    return run


class _Timer:
    """Records one pipeline step, including the SQL it sent."""

    def __init__(
        self,
        run: GateRun,
        reader: GateReader,
        name: str,
        label: str,
        on_step: Callable[[Step], None] | None = None,
    ):
        self._run, self._reader, self._on_step = run, reader, on_step
        self.step = Step(name=name, label=label)
        self.key = ""
        self.rows = 0
        self.detail = ""

    def __enter__(self) -> "_Timer":
        self._start = time.perf_counter()
        return self

    def __exit__(self, exc_type, *_) -> None:
        self.step.ms = int((time.perf_counter() - self._start) * 1000)
        self.step.rows = self.rows
        self.step.detail = self.detail
        self.step.sql = self._reader.sql.get(self.key, "")
        self.step.sample = self._reader.result.get(self.key, [])[:SAMPLE_ROWS]
        self._run.steps.append(self.step)
        if self._on_step:
            self._on_step(self.step)
