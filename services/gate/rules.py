"""Detection: what this revision broke, and where.

Pure functions over rows the reader already fetched. No database, no model, no
network — which is why the rules can be tested exhaustively and tuned for
precision without spending anything.

The input is two things the pipeline has already produced:

    * the fact-level draft diff  (step 2) — which facts changed, and how
    * the dependency traversal   (step 3) — which scenes lean on those facts

The output is a `Break`: one scene, one changed fact, one reason. A `Break` is
not yet a finding. It becomes one in findings.py, once evidence has been
attached and the commitment state looked up — and it is dropped there if the
evidence does not hold up.

THE THREE RULES
---------------
Deliberately three, and deliberately narrow. CLAUDE.md rule 6: three false
positives kill adoption, one missed catch does not.

  A  out_of_order_reference
     A fact moved later in the script (or arrived new), and a scene that depends
     on it now sits BEFORE the scene that establishes it. This is the planted
     break and the reason the product exists: the revision touched 18 and 31,
     and the damage is in 22 and 24, which nobody edited.

  B  restated_fact
     A fact's statement changed, and a scene after it assumes the old wording.
     The dependency survives the edit; what it assumes does not.

  C  dangling_reference
     A fact was removed outright, and a scene that referenced it in the prior
     revision is still in the pages, unedited. Nothing establishes it any more.

WHAT THE RULES DELIBERATELY DO NOT FIRE ON
------------------------------------------
  * A scene that depends on an UNCHANGED fact, even if the ordering looks wrong.
    That is a pre-existing property of the script, not something this week's
    revision broke, and Goldenrod's claim is specifically about the latter.
  * The scene the revision CHANGED. The writer meant to change it; it shoots
    against the current pages and it is correct as scheduled. Flagging it is
    crying wolf about somebody's own edit.
  * Anything whose scene number cannot be placed in script order. A claim that
    rests on ordering is not made when the ordering is unknown.
"""

from __future__ import annotations

from dataclasses import dataclass

from services.gate.scene_order import SceneOrder

RULE_A = "out_of_order_reference"
RULE_B = "restated_fact"
RULE_C = "dangling_reference"


@dataclass(frozen=True)
class ChangedFact:
    """One row of the draft diff (pipeline step 2)."""

    match_key: str
    change: str
    was_scene: str = ""
    now_scene: str = ""
    was_statement: str = ""
    now_statement: str = ""

    @classmethod
    def from_row(cls, row: dict) -> "ChangedFact":
        return cls(
            match_key=str(row.get("match_key", "")),
            change=str(row.get("change", "")),
            was_scene=str(row.get("was_scene", "") or ""),
            now_scene=str(row.get("now_scene", "") or ""),
            was_statement=str(row.get("was_statement", "") or ""),
            now_statement=str(row.get("now_statement", "") or ""),
        )

    @property
    def restated(self) -> bool:
        """The statement itself changed, not only where it lives."""
        return bool(
            self.was_statement
            and self.now_statement
            and self.was_statement != self.now_statement
        )


@dataclass(frozen=True)
class DependencyEdge:
    """One row of the traversal (pipeline step 3): a scene leaning on a fact."""

    scene: str
    fact_key: str
    fact_match_key: str
    fact_kind: str
    statement: str
    established_in_scene: str
    source_line: int
    entity_ids: tuple[str, ...]
    dependency_kind: str
    evidence_line: int
    evidence_quote: str
    synopsis: str = ""

    # Set when this edge was read out of the prior revision and pointed at the
    # current pages by evidence.carry_forward. `cited_revision` names the
    # revision the line number belongs to, empty meaning the current one.
    carried: bool = False
    cited_revision: str = ""

    @classmethod
    def from_row(cls, row: dict) -> "DependencyEdge":
        return cls(
            scene=str(row.get("scene_number", "")),
            fact_key=str(row.get("fact_key", "")),
            fact_match_key=str(row.get("fact_match_key", "")),
            fact_kind=str(row.get("fact_kind", "")),
            statement=str(row.get("statement", "")),
            established_in_scene=str(row.get("established_in_scene", "")),
            source_line=int(row.get("source_line") or 0),
            entity_ids=tuple(row.get("fact_entity_ids") or []),
            dependency_kind=str(row.get("dependency_kind", "")),
            evidence_line=int(row.get("evidence_line") or 0),
            evidence_quote=str(row.get("evidence_quote", "")),
            synopsis=str(row.get("synopsis", "") or ""),
        )


@dataclass(frozen=True)
class Break:
    """One scene broken by one changed fact. Not yet a finding — see findings.py."""

    scene: str
    rule: str
    kind: str
    fact: ChangedFact
    edges: tuple[DependencyEdge, ...]
    claim: str
    action: str

    @property
    def entity_ids(self) -> tuple[str, ...]:
        seen: list[str] = []
        for edge in self.edges:
            for entity in edge.entity_ids:
                if entity not in seen:
                    seen.append(entity)
        return tuple(seen)

    @property
    def establishing_scene(self) -> str:
        return self.fact.now_scene or self.fact.was_scene


# --- claim wording ---------------------------------------------------------
# Templates, not prose from a model. SPEC §4.4: "Determinism matters more than
# prose here. The model fills this; it does not compose paragraphs." Every
# template says only what the attached evidence supports.
MAX_CLAIM = 400


def _trim(statement: str, limit: int = 140) -> str:
    statement = " ".join((statement or "").split()).rstrip(".")
    return statement if len(statement) <= limit else statement[: limit - 1] + "…"


def _claim_a(edge: DependencyEdge, fact: ChangedFact) -> str:
    if fact.change == "ADDED":
        return (
            f"Scene {edge.scene} already plays as though {_trim(edge.statement)}, "
            f"but this revision first establishes that in scene "
            f"{fact.now_scene} — later in the script than {edge.scene}."
        )[:MAX_CLAIM]
    return (
        f"Scene {edge.scene} {'assumes' if edge.dependency_kind == 'assumes' else 'refers to'} "
        f"{_trim(edge.statement)}, but this revision moves that from scene "
        f"{fact.was_scene} to scene {fact.now_scene}, after {edge.scene}."
    )[:MAX_CLAIM]


def _claim_b(edge: DependencyEdge, fact: ChangedFact) -> str:
    return (
        f"Scene {edge.scene} {'assumes' if edge.dependency_kind == 'assumes' else 'refers to'} "
        f"{_trim(fact.was_statement)}, but this revision restated it as "
        f"{_trim(fact.now_statement)}."
    )[:MAX_CLAIM]


def _claim_c(edge: DependencyEdge, fact: ChangedFact) -> str:
    return (
        f"Scene {edge.scene} still refers to {_trim(fact.was_statement or edge.statement)}, "
        f"which this revision cut from scene {fact.was_scene}. "
        f"Nothing in the current pages establishes it."
    )[:MAX_CLAIM]


def _action(scene: str, rule: str) -> str:
    if rule == RULE_C:
        return f"Confirm intentional, or restore the reference the pages no longer support in {scene}."
    if rule == RULE_B:
        return f"Confirm intentional, or bring {scene} into line with the revised wording."
    return f"Confirm intentional, or cut the reference in {scene}."


def _finding_kind(rule: str, fact_kind: str) -> str:
    """Map (rule, fact kind) onto the contract's finding kinds."""
    if rule == RULE_A:
        return "knowledge_state" if fact_kind == "knowledge" else "reference_break"
    if rule == RULE_B:
        return "temporal" if fact_kind == "temporal" else "fact_contradiction"
    return "reference_break"


# --- the rules -------------------------------------------------------------
def detect(
    changed: list[ChangedFact],
    edges: list[DependencyEdge],
    order: SceneOrder,
    *,
    current_scenes: frozenset[str] = frozenset(),
) -> list[Break]:
    """Every break this revision caused, one per (scene, rule, changed fact).

    `edges` is the union of the dependency edges the current revision can
    express and the ones carried forward from the prior revision by
    evidence.carry_forward — see that function for why the second set is not
    optional. Every edge here is already in current-revision coordinates.
    """
    by_key = {fact.match_key: fact for fact in changed}
    grouped: dict[tuple[str, str, str], list[DependencyEdge]] = {}

    def add(rule: str, edge: DependencyEdge, fact: ChangedFact) -> None:
        grouped.setdefault((edge.scene, rule, fact.match_key), []).append(edge)

    for edge in edges:
        fact = by_key.get(edge.fact_match_key)
        if fact is None:
            continue  # the fact did not change; not this revision's doing

        if fact.change == "REMOVED":
            # Nothing establishes it any more. The scene has to still be in the
            # pages for the dangling reference to matter.
            if edge.scene in current_scenes and edge.scene != fact.was_scene:
                add(RULE_C, edge, fact)
            continue

        if edge.scene == fact.now_scene:
            continue  # the scene the revision changed is not a scene it broke

        before = order.precedes(edge.scene, fact.now_scene)
        if before is None:
            continue  # ordering unknown — make no claim that rests on it

        if before and fact.change in ("RELOCATED", "ADDED"):
            add(RULE_A, edge, fact)
        elif not before and fact.restated:
            add(RULE_B, edge, fact)

    breaks: list[Break] = []
    for (scene, rule, match_key), scene_edges in grouped.items():
        fact = by_key[match_key]
        first = scene_edges[0]
        claim = {RULE_A: _claim_a, RULE_B: _claim_b, RULE_C: _claim_c}[rule](first, fact)
        breaks.append(
            Break(
                scene=scene,
                rule=rule,
                kind=_finding_kind(rule, first.fact_kind),
                fact=fact,
                edges=tuple(sorted(scene_edges, key=lambda e: e.evidence_line)),
                claim=claim,
                action=_action(scene, rule),
            )
        )
    return sorted(breaks, key=lambda b: (order.position(b.scene), b.rule))
