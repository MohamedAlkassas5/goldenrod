"""Shared fixtures.

The graph built here is a minimal in-memory structure for exercising the loader,
NOT the production fixture set. The real screenplay, call sheet, seeds and
hand-written answer key still belong in data/fixtures/ per its README.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

PRODUCTION = "test_demo"


def scene(scene_id: str, number: str, synopsis: str, **kw) -> dict[str, Any]:
    return {
        "scene_id": scene_id,
        "scene_number": number,
        "int_ext": kw.get("int_ext", "INT"),
        "day_night": kw.get("day_night", "NIGHT"),
        "location_id": kw.get("location_id", "loc:flat"),
        "page_eighths": kw.get("page_eighths", 8),
        "synopsis": synopsis,
        "entity_ids": kw.get("entity_ids", []),
    }


def base_graph(revision_id: str = "v1") -> dict[str, Any]:
    """A graph shaped like the planted break: the letter fact lives in scene 18,
    scene 24 depends on it, scene 31 is the act-two reveal."""
    return {
        "production_id": PRODUCTION,
        "revision_id": revision_id,
        "scenes": [
            scene("s18", "18", "Rana finds the letter.", entity_ids=["entity:rana", "entity:letter"]),
            scene("s24", "24", "Rana refers to the letter.", entity_ids=["entity:rana"]),
            scene("s31", "31", "The reveal.", entity_ids=["entity:rana", "entity:letter"]),
        ],
        "entities": [
            {"entity_id": "entity:rana", "type": "character", "name": "RANA", "aliases": ["RANA H."]},
            {"entity_id": "entity:letter", "type": "prop", "name": "the letter"},
        ],
        "facts": [
            {
                "fact_id": "f1",
                "statement": "Rana has read the letter",
                "kind": "knowledge",
                "established_in_scene_id": "s18",
                "source_line": 300,
                "entity_ids": ["entity:rana", "entity:letter"],
            }
        ],
        "knowledge_state": [
            {
                "character_entity_id": "entity:rana",
                "fact_id": "f1",
                "scene_id": "s24",
                "knows": True,
                "acquired_via": "reads it in 18",
            }
        ],
        "dependencies": [
            {
                "dependency_id": "d1",
                "from_fact_id": "f1",
                "to_scene_id": "s24",
                "kind": "references",
                "evidence_line": 388,
                "evidence_quote": "You read what she wrote.",
            }
        ],
    }


@pytest.fixture
def graph_v1() -> dict[str, Any]:
    return copy.deepcopy(base_graph("v1"))


@pytest.fixture
def graph_v2() -> dict[str, Any]:
    """The goldenrod revision: the letter fact RELOCATES from scene 18 to 31."""
    g = copy.deepcopy(base_graph("v2"))
    g["facts"][0]["established_in_scene_id"] = "s31"
    g["facts"][0]["source_line"] = 505
    # a text diff sees a moved line; the graph sees Rana no longer knowing in 24
    g["knowledge_state"][0]["knows"] = False
    g["knowledge_state"][0]["acquired_via"] = ""
    return g
