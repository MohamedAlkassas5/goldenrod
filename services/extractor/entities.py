"""Entity identity: names in the pages -> stable `entity:<slug>` ids.

Entity ids are the join between the graph and the money. `commitments` and
`decisions` are keyed on entity_id (db/clickhouse/schema.sql), so if the
Extractor invents a different id for the same picture vehicle than the one the
production office logged against, the commitment lookup silently returns nothing
and every finding ranks as `none`. Identity therefore has to be derived by a
rule, not by a model.

The rule:

    entity_id = "entity:" + slug(name)

    slug: casefold, drop a leading article, replace runs of non-alphanumerics
          with "-", collapse and trim.

So `the Fayoum land` -> `entity:fayoum-land`, `TIN BOX` -> `entity:tin-box`,
`MRS. WADIDA` -> `entity:mrs-wadida`. Surface-form drift between revisions is
absorbed by `aliases`, which is why the column exists on `entities`.

This module is NOT script breakdown (CLAUDE.md's do-not-build list). It resolves
names the parser already found in sluglines and cues, plus whatever the fact
extraction pass names, onto ids. It does not tag elements, and it does not go
looking for props in action lines.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# contracts/graph.schema.json
ENTITY_TYPES = ("character", "location", "prop", "costume", "vehicle", "set", "symbol")

_ARTICLES = ("the ", "a ", "an ")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def slug(name: str) -> str:
    """`MRS. WADIDA (CONT'D)` -> `mrs-wadida`; `the Fayoum land` -> `fayoum-land`."""
    text = re.sub(r"\(.*?\)", " ", name).strip().casefold()
    for article in _ARTICLES:
        if text.startswith(article):
            text = text[len(article):]
            break
    return _NON_ALNUM.sub("-", text).strip("-")


def entity_id(name: str) -> str:
    return f"entity:{slug(name)}"


def location_name(heading_location: str) -> str:
    """The billed location of a slugline: everything before the first comma.

    `FLAT, KITCHEN` and `FLAT, LIVING ROOM` are one practical with a single
    access window and one agreement — the production office commits to the flat,
    not to the kitchen. Splitting them would put the commitment on an id nothing
    is logged against.
    """
    return heading_location.split(",")[0].strip()


@dataclass
class Entity:
    entity_id: str
    type: str
    name: str
    aliases: set[str] = field(default_factory=set)

    def as_contract(self) -> dict:
        return {
            "entity_id": self.entity_id,
            "type": self.type,
            "name": self.name,
            "aliases": sorted(self.aliases),
        }


class EntityRegistry:
    """Production-scoped entity table, built up as scenes are read.

    Entities are production-scoped rather than revision-scoped, matching the
    `entities` table: a character survives a revision, a fact does not.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, Entity] = {}
        self._by_slug: dict[str, str] = {}  # alias slug -> entity_id

    def __len__(self) -> int:
        return len(self._by_id)

    def __contains__(self, key: str) -> bool:
        return key in self._by_id

    def __iter__(self):
        return iter(sorted(self._by_id.values(), key=lambda e: e.entity_id))

    def get(self, entity_id_: str) -> Entity | None:
        return self._by_id.get(entity_id_)

    def resolve(self, name: str, type: str | None = None) -> Entity | None:
        """Find an existing entity by any of its names. Does not create."""
        candidate = slug(name)
        if not candidate:
            return None
        found = self._by_slug.get(candidate)
        if found:
            entity = self._by_id[found]
            return entity if type is None or entity.type == type else None

        # `RANA MOSTAFA` in an action line is the `RANA` of the cue. Only the
        # first token is allowed to match, and only against a single-token id,
        # so `mrs-wadida` can never resolve through its honorific.
        head = candidate.split("-")[0]
        if head != candidate:
            found = self._by_slug.get(head)
            if found:
                entity = self._by_id[found]
                if type is None or entity.type == type:
                    return entity
        return None

    def register(
        self, name: str, type: str, aliases: tuple[str, ...] = ()
    ) -> Entity:
        """Resolve `name`, or add it. Raises on a type the contract does not allow.

        One slug is one entity, whatever type it is offered under. The olive
        grove billed in a slugline and "the olive grove" named as a symbol in a
        fact are the same thing in the world, and the production office logged
        one commitment against one id. Forking them would hang a finding on an
        entity nothing has been paid for, which ranks it `none` — so the first
        registration wins the type, and structural registration (sluglines and
        cues, in `_seed_registry`) always runs first.
        """
        if type not in ENTITY_TYPES:
            raise ValueError(
                f"entity type {type!r} is not in contracts/graph.schema.json "
                f"({', '.join(ENTITY_TYPES)})"
            )
        existing = self.resolve(name, type)
        if existing is None and slug(name):
            candidate = entity_id(name)
            if candidate in self._by_id:
                existing = self._by_id[candidate]
        if existing is not None:
            self._alias(existing, name)
            for alias in aliases:
                self._alias(existing, alias)
            return existing

        if not slug(name):
            raise ValueError(f"entity name {name!r} has no usable identity")

        entity = Entity(entity_id=entity_id(name), type=type, name=name.strip())
        self._by_id[entity.entity_id] = entity
        self._by_slug[slug(name)] = entity.entity_id
        for alias in aliases:
            self._alias(entity, alias)
        return entity

    def _alias(self, entity: Entity, alias: str) -> None:
        key = slug(alias)
        if not key or key == slug(entity.name):
            return
        self._by_slug.setdefault(key, entity.entity_id)
        if self._by_slug[key] == entity.entity_id:
            entity.aliases.add(alias.strip())

    def as_contract(self) -> list[dict]:
        return [e.as_contract() for e in self]
