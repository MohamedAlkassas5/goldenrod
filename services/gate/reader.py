"""Every database read the Gate makes, in one place, through the MCP server.

CLAUDE.md rule 4: all ClickHouse access goes through the ClickHouse MCP server
at runtime. Nothing in this module opens a connection; it is handed a
`ClickHouseMCP` and calls `run_query`.

The four analytical queries live in db/clickhouse/schema.sql and are extracted
at runtime by services/common/queries.py, so the file a judge reads is the file
that runs. This module adds only the two small lookups that are plumbing rather
than analysis — the ledger fetch and the prior-revision resolution — and it
keeps the SQL it actually sent on `GateReader.sql`, so the run can put the query
and its rows on screen (SPEC §7, 0:40–1:15).

Read semantics are not optional here. Every graph table is a ReplacingMergeTree
and needs FINAL; `decisions` is append-only and must NOT get FINAL, because
every row in it is a distinct historical event. See the READ_SEMANTICS note in
services/loader/ingest.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from services.common.mcp_client import ClickHouseMCP
from services.common.queries import bind, get_query
from services.common.sql import lit


@dataclass
class GateReader:
    """Scoped reads for one production. Records the SQL it sent."""

    ch: ClickHouseMCP
    production_id: str
    sql: dict[str, str] = field(default_factory=dict)
    result: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def _run(self, label: str, sql: str) -> list[dict[str, Any]]:
        """Run one statement, keeping both the SQL and what came back.

        Both are kept because the interface shows both: SPEC §7 asks for the
        ClickHouse query AND its returned rows on screen, and a query shown
        without its result is a screenshot of intent rather than of work.
        """
        self.sql[label] = sql
        rows = self.ch.rows(sql)
        self.result[label] = rows
        return rows

    # -- step 2: the fact-level draft diff -----------------------------------
    def draft_diff(self, prior_revision: str, current_revision: str) -> list[dict]:
        return self._run(
            "draft_diff",
            bind(
                get_query("draft_diff"),
                production=self.production_id,
                prior_revision=prior_revision,
                current_revision=current_revision,
            ),
        )

    # -- step 3: traversal ---------------------------------------------------
    def dependent_scenes(self, revision: str, match_keys: list[str]) -> list[dict]:
        if not match_keys:
            return []
        return self._run(
            f"dependent_scenes:{revision}",
            bind(
                get_query("dependent_scenes"),
                production=self.production_id,
                revision=revision,
                match_keys=sorted(set(match_keys)),
            ),
        )

    def changed_facts(self, revision: str, match_keys: list[str]) -> dict[str, dict]:
        """The changed facts as they stand in one revision, by match key.

        The traversal query only returns facts that something depends on. A fact
        that moved past every scene referring to it has no dependants left in the
        new revision — which is exactly the case the Gate is built to catch — so
        the fact side has to be fetched on its own as well.
        """
        if not match_keys:
            return {}
        rows = self._run(
            f"changed_facts:{revision}",
            "SELECT fact_key, fact_match_key, kind, statement, "
            "established_in_scene_number AS established_in_scene, source_line, "
            "entity_ids AS fact_entity_ids FROM facts FINAL "
            f"WHERE production_id = {lit(self.production_id)} "
            f"AND revision_id = {lit(revision)} "
            f"AND has({lit(sorted(set(match_keys)))}, fact_match_key)",
        )
        return {str(r["fact_match_key"]): r for r in rows}

    def scene_hashes(self, revision: str) -> dict[str, str]:
        """text_hash per scene, so an edited scene can be told from an untouched one.

        A scene the writer rewrote is not flagged on the strength of a dependency
        recorded before the rewrite: they may have fixed it, and crying wolf
        about somebody's own edit is the false positive that ends adoption.
        """
        rows = self._run(
            f"scene_hashes:{revision}",
            "SELECT scene_number, text_hash FROM scenes FINAL WHERE production_id = "
            f"{lit(self.production_id)} AND revision_id = {lit(revision)}",
        )
        return {str(r["scene_number"]): str(r["text_hash"]) for r in rows}

    # -- step 4: the ledger --------------------------------------------------
    def commitment_ranking(self, affected_entities: list[str]) -> list[dict]:
        """The demo's query: decision history aggregated against commitment state."""
        if not affected_entities:
            return []
        return self._run(
            "commitment_ranking",
            bind(
                get_query("commitment_ranking"),
                production=self.production_id,
                affected_entities=sorted(set(affected_entities)),
            ),
        )

    def active_decisions(self) -> list[dict]:
        """The live ledger for this production.

        Fetched whole, once per run, rather than per finding: a production's
        active decisions are small, and every finding needs to ask the same two
        questions of them — which one explains this change, and has this finding
        already been marked intentional.
        """
        return self._run(
            "active_decisions",
            "SELECT decision_id, revision_id, scene_id, entity_ids, decision_type, "
            "selected_option, reason, cause_tag, decided_by, decided_at, "
            "intentional_deviation, deviation_reason "
            f"FROM decisions WHERE production_id = {lit(self.production_id)} "
            "AND status = 'active' ORDER BY decided_at DESC",
        )

    # -- step 5: commitment state -------------------------------------------
    def scene_commitments(self, scenes: list[str]) -> list[dict]:
        if not scenes:
            return []
        return self._run(
            "scene_commitments",
            bind(
                get_query("scene_commitments"),
                production=self.production_id,
                scenes=sorted(set(scenes)),
            ),
        )

    # -- step 6: who owns the break -----------------------------------------
    def entity_types(self, entity_ids: list[str]) -> dict[str, str]:
        """Element type per entity id, for department scoping.

        `scene_commitments` already returns a type, but only for entities that
        somebody has committed something against. A finding is owned by the
        department that looks after the elements it is *about*, committed or
        not, so the types have to come from `entities` itself. Production-scoped
        and revision-independent, like the table.
        """
        if not entity_ids:
            return {}
        rows = self._run(
            "entity_types",
            "SELECT entity_id, type FROM entities FINAL "
            f"WHERE production_id = {lit(self.production_id)} "
            f"AND has({lit(sorted(set(entity_ids)))}, entity_id)",
        )
        return {str(r["entity_id"]): str(r["type"]) for r in rows}

    # -- supporting ----------------------------------------------------------
    def previous_revision(self, current_revision: str) -> str:
        """The most recent earlier revision on file.

        contracts/call-sheet.schema.json allows `prior_revision_id` to be
        omitted. "Most recent" is by load time, because a revision id is a
        colour and a date, not something to sort on.
        """
        rows = self._run(
            "previous_revision",
            "SELECT revision_id, max(updated_at) AS loaded_at FROM facts FINAL "
            f"WHERE production_id = {lit(self.production_id)} "
            f"AND revision_id != {lit(current_revision)} "
            "GROUP BY revision_id ORDER BY loaded_at DESC LIMIT 1",
        )
        return str(rows[0]["revision_id"]) if rows else ""
