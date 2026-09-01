"""The reasoning boundary: one call, one scene, one JSON object back.

Everything on the Goldenrod side of this interface is deterministic and testable
without a network — parsing, entity identity, citation checking, graph assembly.
Everything on the far side is the model. Keeping the seam this narrow is what
lets the enforcement in extract.py be tested honestly: the suite drives the whole
Extractor through a backend that returns canned scene payloads, and the code
under test cannot tell the difference.

`StructureOnlyBackend` is not a stub for tests to lean on — it is the `--structure
-only` mode of the CLI, which parses a script into scenes and entities without
spending a model call. Useful for checking a new script parses before paying to
extract it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from services.extractor.fountain import Scene, Script


@dataclass(frozen=True)
class SceneRequest:
    """Everything the model is allowed to see for one scene."""

    production_id: str
    revision_id: str
    script: Script
    scene: Scene
    known_facts: list[dict] = field(default_factory=list)
    known_entities: list[dict] = field(default_factory=list)


@runtime_checkable
class ExtractionBackend(Protocol):
    """One scene in, one SCENE_SCHEMA-shaped dict out."""

    def extract_scene(self, request: SceneRequest) -> dict: ...


class StructureOnlyBackend:
    """Parses and resolves entities, extracts no facts. Costs nothing."""

    name = "structure-only"

    def extract_scene(self, request: SceneRequest) -> dict:
        return {"facts": [], "knowledge_state": [], "dependencies": []}
