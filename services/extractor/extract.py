"""The Extractor: screenplay + revision id -> a contract-valid film graph.

    parse -> per-scene model pass -> CHECK EVERY CITATION -> assemble -> validate

The middle step is the one that matters. CLAUDE.md rule 1 says a finding needs
evidence and that the rule is enforced in code, not in a prompt. A finding can
only cite what the graph holds, so the rule has to bite here first: a fact whose
quote is not on the line it cites does not get written, a dependency whose
evidence line is outside the scene does not get written, and a knowledge_state
entry pointing at a fact that does not exist does not get written. Nothing is
invented to fill a gap.

There is one repair, and it is deliberately narrow: if a quote is not on the
cited line but appears on exactly one other line of the same scene, the line
number is corrected to that line. The evidence still comes from the file — only
the model's arithmetic is fixed. Ambiguous or absent, and the item is dropped.

Everything dropped is reported, per scene and with a reason, by
`ExtractionReport`. That is not decoration: it is how you tune for precision
(CLAUDE.md rule 6) without guessing at what the pass is throwing away.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from services.common.text import normalise as _normalise
from services.extractor.backend import ExtractionBackend, SceneRequest
from services.extractor.entities import EntityRegistry, location_name
from services.extractor.fountain import Scene, Script
from services.extractor.prompt import DEPENDENCY_KINDS, FACT_KINDS

# contracts/graph.schema.json
MAX_STATEMENT = 300
MAX_QUOTE = 300
MAX_SYNOPSIS = 300

# Shortest quote we will accept as identifying a line we were not pointed at.
MIN_SNAP_QUOTE = 6


class ExtractionError(RuntimeError):
    """The extraction produced something that is not a valid graph."""


@dataclass(frozen=True)
class Dropped:
    """One thing the model returned that the file did not support."""

    scene_number: str
    item: str  # fact | knowledge_state | dependency | entity
    reason: str
    detail: str = ""

    def __str__(self) -> str:
        tail = f" — {self.detail}" if self.detail else ""
        return f"scene {self.scene_number}: {self.item} dropped, {self.reason}{tail}"


@dataclass
class ExtractionReport:
    """What the pass produced and what it refused. Print it; it is the tuning loop."""

    production_id: str
    revision_id: str
    scenes: int = 0
    facts: int = 0
    knowledge_state: int = 0
    dependencies: int = 0
    entities: int = 0
    snapped: int = 0
    dropped: list[Dropped] = field(default_factory=list)

    @property
    def scenes_with_a_fact(self) -> set[str]:
        return self._scenes_with_a_fact

    def __post_init__(self) -> None:
        self._scenes_with_a_fact: set[str] = set()

    def summary(self) -> str:
        yielded = len(self._scenes_with_a_fact)
        pct = (100 * yielded / self.scenes) if self.scenes else 0
        lines = [
            f"{self.production_id} @ {self.revision_id}",
            f"  scenes            {self.scenes}",
            f"  facts             {self.facts}",
            f"  knowledge_state   {self.knowledge_state}",
            f"  dependencies      {self.dependencies}",
            f"  entities          {self.entities}",
            f"  scenes with >=1 fact  {yielded}/{self.scenes} ({pct:.0f}%)",
        ]
        if self.snapped:
            lines.append(f"  citations corrected to the right line: {self.snapped}")
        if self.dropped:
            lines.append(f"  dropped (uncitable or unresolvable): {len(self.dropped)}")
            lines.extend(f"    {d}" for d in self.dropped)
        return "\n".join(lines)


# --- citation checking -----------------------------------------------------
# Normalisation is shared with the Gate (services/common/text.py); the matching
# policy below is not — see that module's docstring for why.
def _matches(quote: str, line_text: str) -> bool:
    a, b = _normalise(quote), _normalise(line_text)
    if not a or not b:
        return False
    return a in b or b in a


def locate(script: Script, scene: Scene, cited_line: int, quote: str) -> tuple[int, bool] | None:
    """(line, snapped) for a verified citation, or None if the file does not back it.

    The cited line wins if it matches. Otherwise the quote must identify exactly
    one other line in the same scene; anything else is a citation we cannot
    stand behind, so it goes.
    """
    in_scene = scene.start_line <= cited_line <= scene.end_line
    if in_scene and _matches(quote, script.line_text(cited_line)):
        return cited_line, False

    if len(_normalise(quote)) < MIN_SNAP_QUOTE:
        return None
    hits = [
        n
        for n in range(scene.start_line, scene.end_line + 1)
        if _matches(quote, script.line_text(n))
    ]
    if len(hits) == 1:
        return hits[0], True
    return None


# --- one scene -------------------------------------------------------------
@dataclass
class _SceneResult:
    facts: list[dict] = field(default_factory=list)
    knowledge_state: list[dict] = field(default_factory=list)
    dependencies: list[dict] = field(default_factory=list)
    entity_ids: set[str] = field(default_factory=set)


def _resolve_entities(
    registry: EntityRegistry, refs: list[dict], scene_number: str, report: ExtractionReport
) -> list[str]:
    resolved: list[str] = []
    for ref in refs or []:
        name, type_ = str(ref.get("name", "")).strip(), str(ref.get("type", "")).strip()
        try:
            entity = registry.register(name, type_)
        except ValueError as exc:
            report.dropped.append(Dropped(scene_number, "entity", str(exc), name))
            continue
        if entity.entity_id not in resolved:
            resolved.append(entity.entity_id)
    return resolved


def enforce_scene(
    script: Script,
    scene: Scene,
    payload: dict,
    registry: EntityRegistry,
    fact_handles: dict[str, dict],
    report: ExtractionReport,
) -> _SceneResult:
    """Check one scene's model output against the file. Drops, never invents."""
    result = _SceneResult()
    local: list[str] = []  # this scene's fact handles, in the model's own order

    # -- facts
    for index, fact in enumerate(payload.get("facts") or []):
        kind = str(fact.get("kind", ""))
        if kind not in FACT_KINDS:
            report.dropped.append(
                Dropped(scene.scene_number, "fact", "kind is not in the contract", kind)
            )
            local.append("")
            continue

        quote = str(fact.get("quote", ""))
        located = locate(script, scene, int(fact.get("source_line") or 0), quote)
        if located is None:
            report.dropped.append(
                Dropped(
                    scene.scene_number,
                    "fact",
                    "quote is not on the line it cites",
                    f"line {fact.get('source_line')}: {quote[:60]!r}",
                )
            )
            local.append("")
            continue
        line, snapped = located
        report.snapped += int(snapped)

        entity_ids = _resolve_entities(
            registry, fact.get("entities") or [], scene.scene_number, report
        )
        if not entity_ids:
            report.dropped.append(
                Dropped(
                    scene.scene_number,
                    "fact",
                    "no resolvable entity, so nothing can depend on it",
                    str(fact.get("statement", ""))[:60],
                )
            )
            local.append("")
            continue

        handle = f"f{scene.scene_number}.{len(result.facts) + 1}"
        row = {
            "fact_id": handle,
            "statement": str(fact.get("statement", "")).strip()[:MAX_STATEMENT],
            "kind": kind,
            "established_in_scene_id": scene.scene_id,
            "source_line": line,
            "entity_ids": entity_ids,
        }
        if not row["statement"]:
            report.dropped.append(
                Dropped(scene.scene_number, "fact", "empty statement", handle)
            )
            local.append("")
            continue

        result.facts.append(row)
        result.entity_ids.update(entity_ids)
        local.append(handle)
        fact_handles[handle] = {
            "handle": handle,
            "kind": kind,
            "scene_number": scene.scene_number,
            "statement": row["statement"],
            "entity_names": [
                registry.get(e).name for e in entity_ids if registry.get(e)
            ],
        }

    def resolve_ref(ref: str) -> str:
        """'#0' -> this scene's fact at that index; 'f18.2' -> an earlier handle."""
        ref = str(ref or "").strip()
        if ref.startswith("#"):
            try:
                index = int(ref[1:])
            except ValueError:
                return ""
            return local[index] if 0 <= index < len(local) else ""
        return ref if ref in fact_handles else ""

    # -- knowledge_state
    seen_knowledge: set[tuple[str, str]] = set()
    for entry in payload.get("knowledge_state") or []:
        handle = resolve_ref(entry.get("fact_ref"))
        if not handle:
            report.dropped.append(
                Dropped(
                    scene.scene_number,
                    "knowledge_state",
                    "references a fact that does not exist",
                    str(entry.get("fact_ref")),
                )
            )
            continue
        character = registry.resolve(str(entry.get("character", "")), "character")
        if character is None:
            report.dropped.append(
                Dropped(
                    scene.scene_number,
                    "knowledge_state",
                    "not a known character",
                    str(entry.get("character")),
                )
            )
            continue
        key = (character.entity_id, handle)
        if key in seen_knowledge:
            continue
        seen_knowledge.add(key)
        knows = bool(entry.get("knows"))
        result.knowledge_state.append(
            {
                "character_entity_id": character.entity_id,
                "fact_id": handle,
                "scene_id": scene.scene_id,
                "knows": knows,
                "acquired_via": str(entry.get("acquired_via", "")).strip() if knows else "",
            }
        )
        result.entity_ids.add(character.entity_id)

    # -- dependencies
    seen_dependencies: set[tuple[str, str]] = set()
    for entry in payload.get("dependencies") or []:
        kind = str(entry.get("kind", ""))
        if kind not in DEPENDENCY_KINDS:
            report.dropped.append(
                Dropped(scene.scene_number, "dependency", "kind is not in the contract", kind)
            )
            continue
        handle = resolve_ref(entry.get("fact_ref"))
        if not handle:
            report.dropped.append(
                Dropped(
                    scene.scene_number,
                    "dependency",
                    "references a fact that does not exist",
                    str(entry.get("fact_ref")),
                )
            )
            continue
        if fact_handles[handle]["scene_number"] == scene.scene_number:
            report.dropped.append(
                Dropped(
                    scene.scene_number,
                    "dependency",
                    "points at a fact from its own scene, so nothing second-order",
                    handle,
                )
            )
            continue

        quote = str(entry.get("evidence_quote", ""))
        located = locate(script, scene, int(entry.get("evidence_line") or 0), quote)
        if located is None:
            report.dropped.append(
                Dropped(
                    scene.scene_number,
                    "dependency",
                    "evidence quote is not on the line it cites",
                    f"line {entry.get('evidence_line')}: {quote[:60]!r}",
                )
            )
            continue
        line, snapped = located
        report.snapped += int(snapped)

        key = (handle, kind)
        if key in seen_dependencies:
            continue
        seen_dependencies.add(key)
        result.dependencies.append(
            {
                "dependency_id": f"d{scene.scene_number}.{len(result.dependencies) + 1}",
                "from_fact_id": handle,
                "to_scene_id": scene.scene_id,
                "kind": kind,
                "evidence_line": line,
                "evidence_quote": script.line_text(line).strip()[:MAX_QUOTE],
            }
        )

    return result


# --- the pass --------------------------------------------------------------
def _synopsis(scene: Scene) -> str:
    """First action line, else the heading. A one-line label, not a summary."""
    for element in scene.elements:
        if element.type == "action":
            return element.text[:MAX_SYNOPSIS]
    return scene.heading[:MAX_SYNOPSIS]


def _seed_registry(registry: EntityRegistry, scene: Scene) -> set[str]:
    """Cast and location for one scene, straight off the pages."""
    ids: set[str] = set()
    billed = location_name(scene.location)
    if billed:
        ids.add(registry.register(billed, "location", aliases=(scene.location,)).entity_id)
    for cue in scene.characters:
        ids.add(registry.register(cue, "character").entity_id)
    return ids


def extract_graph(
    script: Script,
    production_id: str,
    revision_id: str,
    backend: ExtractionBackend,
    *,
    on_scene: Callable[[Scene, ExtractionReport], None] | None = None,
) -> tuple[dict, ExtractionReport]:
    """Run the Extractor over a parsed script. Returns (graph, report).

    Scenes are visited in script order so that a scene can only depend on facts
    established before it — which is also the only order in which the running
    `established facts` context makes sense to the model.
    """
    registry = EntityRegistry()
    report = ExtractionReport(production_id=production_id, revision_id=revision_id)
    report.scenes = len(script.scenes)

    scene_rows: list[dict] = []
    facts: list[dict] = []
    knowledge: list[dict] = []
    dependencies: list[dict] = []
    fact_handles: dict[str, dict] = {}

    for scene in script.scenes:
        structural = _seed_registry(registry, scene)

        payload = backend.extract_scene(
            SceneRequest(
                production_id=production_id,
                revision_id=revision_id,
                script=script,
                scene=scene,
                known_facts=list(fact_handles.values()),
                known_entities=[
                    {"name": e.name, "type": e.type} for e in registry
                ],
            )
        )
        result = enforce_scene(script, scene, payload, registry, fact_handles, report)

        facts.extend(result.facts)
        knowledge.extend(result.knowledge_state)
        dependencies.extend(result.dependencies)
        if result.facts:
            report.scenes_with_a_fact.add(scene.scene_number)
        report.facts, report.knowledge_state = len(facts), len(knowledge)
        report.dependencies, report.entities = len(dependencies), len(registry)

        scene_rows.append(
            {
                "scene_id": scene.scene_id,
                "scene_number": scene.scene_number,
                "int_ext": scene.int_ext,
                "location_id": next(
                    (
                        e
                        for e in sorted(structural)
                        if (registry.get(e) and registry.get(e).type == "location")
                    ),
                    "",
                ),
                "day_night": scene.day_night,
                "page_eighths": _page_eighths(scene),
                "synopsis": _synopsis(scene),
                "text_hash": _text_hash(script, scene),
                "entity_ids": sorted(structural | result.entity_ids),
            }
        )
        if on_scene:
            on_scene(scene, report)

    graph = {
        "production_id": production_id,
        "revision_id": revision_id,
        "scenes": scene_rows,
        "entities": registry.as_contract(),
        "facts": facts,
        "knowledge_state": knowledge,
        "dependencies": dependencies,
    }
    report.facts = len(facts)
    report.knowledge_state = len(knowledge)
    report.dependencies = len(dependencies)
    report.entities = len(registry)

    _validate(graph)
    return graph, report


def _page_eighths(scene: Scene) -> int:
    """Rough length in eighths. A page is ~55 lines; production counts in eighths."""
    lines = scene.end_line - scene.start_line + 1
    return max(1, round(lines * 8 / 55))


def _text_hash(script: Script, scene: Scene) -> str:
    import hashlib

    body = "\n".join(
        script.lines[n - 1].rstrip() for n in range(scene.start_line, scene.end_line + 1)
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def _validate(graph: dict) -> None:
    """The contract, then referential integrity the contract cannot express."""
    from services.loader.ingest import IngestError, resolve, validate_graph

    try:
        validate_graph(graph)
        resolve(graph)  # resolves scene_id/fact_id and fails on anything dangling
    except IngestError as exc:
        raise ExtractionError(
            f"the Extractor produced a graph the loader will not accept:\n{exc}"
        ) from exc
