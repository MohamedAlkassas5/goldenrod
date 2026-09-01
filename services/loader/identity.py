"""Fact identity across revisions.

The rule, as documented in db/clickhouse/schema.sql:

    fact_key = (production_id, kind, established_in_scene_number, sorted(entity_ids))

Deliberately NOT part of identity — these are passthrough handles only, and
nothing in this package may join on them:

    fact_id        emitted per-revision by the Extractor, not stable
    scene_id       ditto; scene_number is the stable coordinate
    dependency_id  ditto

Why scene_number is stable: Goldenrod only runs on locked scripts. Once a script
is locked for production, scene numbers never change — inserted scenes take
letters (24A), cut scenes become "24 OMITTED". A goldenrod revision is by
definition a production-driven change to an already-locked script.

These functions are pure. They mirror, character for character, the MATERIALIZED
expression on the `facts` table:

    concat(production_id,'|',kind,'|',established_in_scene_number,'|',
           arrayStringConcat(arraySort(entity_ids),','),'#',toString(collision_ord))

That duplication is deliberate — the loader needs the key in order to resolve
knowledge_state and dependencies onto it before writing. It is also verified:
`ingest` re-reads the database-computed keys after every load and fails loudly if
the two implementations ever disagree. See ingest.verify_identity_agreement.

Sort-order note: ClickHouse arraySort orders strings by UTF-8 byte value and
Python sorts str by code point. For UTF-8 those two orderings are identical, so
the keys agree for non-ASCII entity ids as well as ASCII.
"""

from __future__ import annotations

from typing import Any, Iterable

KEY_SEP = "|"
ENTITY_SEP = ","
ORD_SEP = "#"


def normalise_entity_ids(entity_ids: Iterable[str] | None) -> list[str]:
    """Sorted, de-duplicated entity ids.

    Sorting removes the Extractor's array-ordering nondeterminism. De-duplication
    keeps identity stable when one revision happens to mention an entity twice
    and the next does not. The loader writes this normalised array, so the
    database's MATERIALIZED expression sees exactly what we computed from.
    """
    return sorted(set(entity_ids or []))


def identity_components(
    production_id: str,
    kind: str,
    established_in_scene_number: str,
    entity_ids: Iterable[str] | None,
) -> tuple[str, str, str, tuple[str, ...]]:
    """The four things that identify a fact, before collision numbering."""
    return (
        production_id,
        kind,
        established_in_scene_number,
        tuple(normalise_entity_ids(entity_ids)),
    )


def fact_match_key(
    production_id: str,
    kind: str,
    entity_ids: Iterable[str] | None,
) -> str:
    """Identity minus the scene. Drives tier 2 of the diff (RELOCATED).

    A fact moving scenes is what silently invalidates knowledge_state, and it is
    invisible to any text diff.
    """
    entities = ENTITY_SEP.join(normalise_entity_ids(entity_ids))
    return KEY_SEP.join([production_id, kind, entities])


def fact_key(
    production_id: str,
    kind: str,
    established_in_scene_number: str,
    entity_ids: Iterable[str] | None,
    collision_ord: int = 1,
) -> str:
    """The full cross-revision identity of one fact."""
    entities = ENTITY_SEP.join(normalise_entity_ids(entity_ids))
    base = KEY_SEP.join([production_id, kind, established_in_scene_number, entities])
    return f"{base}{ORD_SEP}{collision_ord}"


def assign_collision_ords(facts: list[dict[str, Any]]) -> list[int]:
    """Deterministic collision_ord, parallel to the input list.

    Two facts in one revision can share (kind, scene, entity-set). They are
    ordered by source_line ascending and numbered from 1, per schema.sql.

    source_line alone is not a total order — two facts can be extracted from the
    same line — so `statement` then the fact's position in the input break
    remaining ties. Without a total order the numbering would depend on dict
    iteration order and identity would drift between two loads of the same graph.
    """
    grouped: dict[tuple, list[int]] = {}
    for index, fact in enumerate(facts):
        key = identity_components(
            fact["production_id"],
            fact["kind"],
            fact["established_in_scene_number"],
            fact.get("entity_ids"),
        )
        grouped.setdefault(key, []).append(index)

    ords = [1] * len(facts)
    for indices in grouped.values():
        ordered = sorted(
            indices,
            key=lambda i: (
                int(facts[i].get("source_line") or 0),
                str(facts[i].get("statement") or ""),
                i,
            ),
        )
        for position, index in enumerate(ordered, start=1):
            ords[index] = position
    return ords
