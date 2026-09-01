"""Extractor — script to graph.

One of the two agents (CLAUDE.md: "Two agents only"). Batch, not interactive:
it runs on ingest and on every new revision, one structured-output pass per
scene, against the fixed schema in `prompt.SCENE_SCHEMA`.

    from services.extractor import parse_fountain, extract_graph, GeminiBackend

    script = parse_fountain(path.read_text(encoding="utf-8"))
    graph, report = extract_graph(script, "fayoum", "goldenrod-2026-08-29", GeminiBackend())

The graph it returns is valid against contracts/graph.schema.json and accepted by
services.loader — that is checked before it is returned, not hoped for.
"""

from services.extractor.backend import (
    ExtractionBackend,
    SceneRequest,
    StructureOnlyBackend,
)
from services.extractor.entities import EntityRegistry, entity_id, slug
from services.extractor.extract import (
    ExtractionError,
    ExtractionReport,
    extract_graph,
    locate,
)
from services.extractor.fountain import FountainError, Script, parse_fountain
from services.extractor.gemini import GeminiBackend, GeminiError

__all__ = [
    "EntityRegistry",
    "ExtractionBackend",
    "ExtractionError",
    "ExtractionReport",
    "FountainError",
    "GeminiBackend",
    "GeminiError",
    "SceneRequest",
    "Script",
    "StructureOnlyBackend",
    "entity_id",
    "extract_graph",
    "locate",
    "parse_fountain",
    "slug",
]
