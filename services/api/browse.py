"""Read-only browse queries for the interface.

Kept apart from `services/gate/reader.py` on purpose. That module is the reads
the *check* makes — the pipeline, in order, with the analytical queries the demo
puts on screen. This one is the reads a *person* makes when they click into the
graph to see what Goldenrod knows: scenes, facts, who knows what, and the ledger
with its reasons.

Everything here goes through the ClickHouse MCP server, like every other
statement in this codebase.

Read semantics, which are not optional: the graph tables are ReplacingMergeTree
and need FINAL, `decisions` is append-only and must not get it. See the
READ_SEMANTICS note in services/loader/ingest.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from services.common.mcp_client import ClickHouseMCP
from services.common.sql import lit

# A browse screen is for reading, not for exporting. Capped so a feature-length
# script cannot hand the interface fifty thousand rows.
LIMIT = 500


@dataclass
class Browser:
    """Read-only view of one production's graph and ledger."""

    ch: ClickHouseMCP
    production_id: str

    def _scope(self, revision_id: str = "") -> str:
        scope = f"production_id = {lit(self.production_id)}"
        if revision_id:
            scope += f" AND revision_id = {lit(revision_id)}"
        return scope

    def scenes(self, revision_id: str) -> list[dict[str, Any]]:
        return self.ch.rows(
            "SELECT scene_number, int_ext, day_night, location_id, page_eighths, "
            "synopsis, entity_ids FROM scenes FINAL "
            f"WHERE {self._scope(revision_id)} LIMIT {LIMIT}"
        )

    def entities(self) -> list[dict[str, Any]]:
        return self.ch.rows(
            "SELECT entity_id, type, name, aliases FROM entities FINAL "
            f"WHERE {self._scope()} ORDER BY type, entity_id LIMIT {LIMIT}"
        )

    def facts(self, revision_id: str) -> list[dict[str, Any]]:
        return self.ch.rows(
            "SELECT fact_key, kind, established_in_scene_number AS scene, statement, "
            "source_line, entity_ids FROM facts FINAL "
            f"WHERE {self._scope(revision_id)} LIMIT {LIMIT}"
        )

    def knowledge_state(self, revision_id: str) -> list[dict[str, Any]]:
        """Who knows what, by when. SPEC §4.1: this is the one that catches things."""
        return self.ch.rows(
            "SELECT character_entity_id, fact_key, scene_number, knows, acquired_via "
            f"FROM knowledge_state FINAL WHERE {self._scope(revision_id)} LIMIT {LIMIT}"
        )

    def dependencies(self, revision_id: str) -> list[dict[str, Any]]:
        return self.ch.rows(
            "SELECT from_fact_key, to_scene_number, kind, evidence_line, evidence_quote "
            f"FROM dependencies FINAL WHERE {self._scope(revision_id)} LIMIT {LIMIT}"
        )

    def decisions(self) -> list[dict[str, Any]]:
        """The ledger, newest first. No FINAL: every row is a historical event."""
        return self.ch.rows(
            "SELECT toString(decision_id) AS decision_id, scene_id, entity_ids, "
            "decision_type, selected_option, alternatives, reason, cause_tag, "
            "decided_by, decided_at, status, intentional_deviation, deviation_reason "
            f"FROM decisions WHERE production_id = {lit(self.production_id)} "
            f"ORDER BY decided_at DESC LIMIT {LIMIT}"
        )

    def commitments(self) -> list[dict[str, Any]]:
        """Commitment state, ranked by the one ranking implementation."""
        return self.ch.rows(
            "SELECT entity_id, scene_id, state, commitmentRank(state) AS commitment_rank, "
            "cost_band, committed_at, notes FROM commitments FINAL "
            f"WHERE production_id = {lit(self.production_id)} "
            f"ORDER BY commitment_rank ASC, scene_id ASC LIMIT {LIMIT}"
        )

    def revisions(self) -> list[dict[str, Any]]:
        """Every revision on file, newest load first."""
        return self.ch.rows(
            "SELECT revision_id, count() AS facts, max(updated_at) AS loaded_at "
            f"FROM facts FINAL WHERE production_id = {lit(self.production_id)} "
            "GROUP BY revision_id ORDER BY loaded_at DESC"
        )

    def graph(self, revision_id: str) -> dict[str, Any]:
        """Everything the graph view needs, in one call."""
        return {
            "production_id": self.production_id,
            "revision_id": revision_id,
            "revisions": self.revisions(),
            "scenes": self.scenes(revision_id),
            "entities": self.entities(),
            "facts": self.facts(revision_id),
            "knowledge_state": self.knowledge_state(revision_id),
            "dependencies": self.dependencies(revision_id),
            "decisions": self.decisions(),
            "commitments": self.commitments(),
        }
