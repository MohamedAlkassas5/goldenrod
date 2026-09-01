"""Script order from locked scene numbers.

The Gate has to answer one question constantly: *is this scene before or after
that one?* "Rana refers to the letter in 24, but she does not read it until 31"
is only a finding because 24 comes before 31.

Nothing in the database stores an ordinal. It does not need to, because on a
locked script the scene number IS the ordering — that is the whole point of
locking. db/clickhouse/schema.sql already leans on this for fact identity:

    "Once a script is locked for production, scene numbers never change —
     inserted scenes take letters (24A), cut scenes become '24 OMITTED'."

So order is derived from the number, by the same convention the production
office uses:

    A24 -> 24 -> 24A -> 24B -> 25

A trailing letter is a scene inserted *after* its base number; a leading letter
is one inserted *before* it. Both conventions are in use, and both sort
correctly here.

A number this rule cannot parse is not guessed at: it sorts after everything
parseable, in stable string order, and `SceneOrder.knows` reports False so a
caller can decline to make a claim that rests on the ordering. Precision over
recall (CLAUDE.md rule 6) — a scene we cannot place is a scene we do not flag.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

# A locked scene number: optional letters, digits, optional letters.
_LOCKED = re.compile(r"^(?P<prefix>[A-Za-z]*)(?P<number>\d+)(?P<suffix>[A-Za-z]*)$")

# Unparseable numbers sort after every parseable one, without crashing a run.
_UNPARSEABLE = 10**9


def scene_sort_key(scene_number: str) -> tuple[int, int, str, str, str]:
    """Sort key placing a locked scene number in script order.

    The tuple is (base number, insertion side, prefix, suffix, raw). "Insertion
    side" is -1 for a leading-letter scene, 0 for a plain one and 1 for a
    trailing-letter scene, which is what puts A24 before 24 before 24A.
    """
    raw = (scene_number or "").strip()
    match = _LOCKED.match(raw.split()[0] if raw else "")
    if not match:
        return (_UNPARSEABLE, 0, "", "", raw)
    prefix = match.group("prefix").upper()
    suffix = match.group("suffix").upper()
    side = -1 if prefix else (1 if suffix else 0)
    return (int(match.group("number")), side, prefix, suffix, raw)


def is_locked_number(scene_number: str) -> bool:
    """Whether this scene number can be placed in script order at all."""
    return scene_sort_key(scene_number)[0] != _UNPARSEABLE


@dataclass(frozen=True)
class SceneOrder:
    """Script order over one revision's scenes.

    Built from the `scenes` table for the revision under check, so the ordering
    is the pages' own and not an assumption about numbering density.
    """

    numbers: tuple[str, ...]

    @classmethod
    def of(cls, scene_numbers: Iterable[str]) -> "SceneOrder":
        return cls(tuple(sorted({str(n) for n in scene_numbers}, key=scene_sort_key)))

    def knows(self, scene_number: str) -> bool:
        return is_locked_number(scene_number)

    def position(self, scene_number: str) -> tuple:
        return scene_sort_key(scene_number)

    def precedes(self, earlier: str, later: str) -> bool | None:
        """Is `earlier` before `later` in the script? None if it cannot be told."""
        if not (self.knows(earlier) and self.knows(later)):
            return None
        return self.position(earlier) < self.position(later)

    def sorted(self, scene_numbers: Iterable[str]) -> list[str]:
        return sorted(scene_numbers, key=scene_sort_key)
