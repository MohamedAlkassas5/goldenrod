"""The Extractor's fixed schema and its prompt. One pass per scene.

SPEC §3: "Extractor — script → graph. [...] Structured output against a fixed
schema. Batch, not interactive." SPEC §6, day 2–3: "Fixed schema, structured
output, one pass per scene."

The model is asked for exactly one thing: the facts a scene establishes, who
knows them, and which earlier fact this scene leans on. Everything structural —
scene numbers, sluglines, cues, dialogue, line numbers — is parsed
deterministically in fountain.py and handed to the model as context. It is never
asked to reproduce any of it.

`SCENE_SCHEMA` is the response schema. It is passed to the model as its
structured-output schema AND validated locally on the way back, because a
schema-constrained decode is a strong prior, not a guarantee. Everything the
model returns is then checked against the actual file in extract.py: a fact whose
quote is not on the line it cites is dropped, not repaired into existence.

Fact references
---------------
A scene's knowledge_state and dependencies have to point at facts. Two forms:

    "#0"      the fact at index 0 of THIS response's `facts` array
    "f18.2"   a handle from `established_facts`, established in an earlier scene

That keeps the model from inventing identifiers, and both forms are resolvable
without trusting it.
"""

from __future__ import annotations

from services.extractor.entities import ENTITY_TYPES
from services.extractor.fountain import Scene, Script, numbered

FACT_KINDS = ("world", "relationship", "possession", "knowledge", "physical", "temporal")
DEPENDENCY_KINDS = ("references", "assumes", "contradicts_if_changed")

# The fixed schema. Kept to the OpenAPI subset that structured-output decoding
# accepts, so the same dict is both the model's response schema and what the
# response is validated against locally.
SCENE_SCHEMA: dict = {
    "type": "object",
    "required": ["facts", "knowledge_state", "dependencies"],
    "properties": {
        "facts": {
            "type": "array",
            "description": "What THIS scene establishes for the first time.",
            "items": {
                "type": "object",
                "required": ["statement", "kind", "source_line", "quote", "entities"],
                "properties": {
                    "statement": {
                        "type": "string",
                        "description": "One sentence, present tense, checkable against the line.",
                    },
                    "kind": {"type": "string", "enum": list(FACT_KINDS)},
                    "source_line": {
                        "type": "integer",
                        "description": "The numbered line in this scene that establishes it.",
                    },
                    "quote": {
                        "type": "string",
                        "description": "The text of that line, copied exactly.",
                    },
                    "entities": {
                        "type": "array",
                        "description": "Who or what the fact is about. At least one.",
                        "items": {
                            "type": "object",
                            "required": ["name", "type"],
                            "properties": {
                                "name": {"type": "string"},
                                "type": {"type": "string", "enum": list(ENTITY_TYPES)},
                            },
                        },
                    },
                },
            },
        },
        "knowledge_state": {
            "type": "array",
            "description": "Who knows what, as of the end of this scene.",
            "items": {
                "type": "object",
                "required": ["character", "fact_ref", "knows"],
                "properties": {
                    "character": {"type": "string", "description": "Cue name, e.g. RANA."},
                    "fact_ref": {"type": "string", "description": "'#0' or a handle like 'f18.2'."},
                    "knows": {"type": "boolean"},
                    "acquired_via": {
                        "type": "string",
                        "description": "How they learned it. Empty when they do not know it.",
                    },
                },
            },
        },
        "dependencies": {
            "type": "array",
            "description": "Earlier facts THIS scene leans on. Cross-scene only.",
            "items": {
                "type": "object",
                "required": ["fact_ref", "kind", "evidence_line", "evidence_quote"],
                "properties": {
                    "fact_ref": {
                        "type": "string",
                        "description": "Handle of an EARLIER scene's fact, e.g. 'f18.2'.",
                    },
                    "kind": {"type": "string", "enum": list(DEPENDENCY_KINDS)},
                    "evidence_line": {
                        "type": "integer",
                        "description": "The line in THIS scene that does the leaning.",
                    },
                    "evidence_quote": {
                        "type": "string",
                        "description": "The text of that line, copied exactly.",
                    },
                },
            },
        },
    },
}

SYSTEM_PROMPT = """\
You are the Extractor in Goldenrod, a pre-commitment check used by a production \
office. You read one scene of a locked screenplay and return the facts it \
establishes, who knows them, and which earlier facts it depends on.

You are not writing coverage, notes, or a breakdown. You do not judge the \
writing. You record what the pages state, so that when a revision changes one of \
these facts the production office can be shown every other scene that no longer \
holds.

Rules, in order of importance:

1. CITE OR DROP. Every fact carries the line number that establishes it and that \
line's text copied exactly. Every dependency carries the line in this scene that \
does the leaning. Anything you cannot cite from the lines shown, leave out. A \
claim without a line is deleted downstream, so it only wastes the slot.
2. PRECISION OVER RECALL. This runs the night before a shoot day. Three findings \
that turn out to be nothing will get the tool switched off; one missed catch will \
not. When a fact is arguable, do not record it.
3. STATE, DO NOT INTERPRET. "Rana's mother sold the Fayoum land" is a fact. \
"Rana feels betrayed" is not — it is unfalsifiable and nothing can be checked \
against it.
4. FIRST ESTABLISHMENT ONLY. If a fact was already established in an earlier \
scene, do not record it again — record a dependency on it instead.
5. KNOWLEDGE IS THE POINT. Who knows what, and when they learned it, is what \
catches the expensive breaks. Record it for a character whenever this scene \
settles it, including when the scene establishes that they do NOT know.
6. DEPENDENCIES ARE CROSS-SCENE. Record one only when THIS scene leans on a fact \
established in an EARLIER scene. A scene leaning on its own fact tells nobody \
anything.

Fact references:
  "#0"    the fact at index 0 of your own `facts` array, in the order you return it
  "f18.2" a handle from the established facts you were given

Return JSON matching the schema. No prose, no markdown, no explanation."""


def _established_block(known_facts: list[dict]) -> str:
    if not known_facts:
        return "(none yet — this is the first scene)"
    return "\n".join(
        f"  {f['handle']}  [{f['kind']}] scene {f['scene_number']}: {f['statement']}"
        f"  ({', '.join(f['entity_names']) or 'no entities'})"
        for f in known_facts
    )


def _entity_block(known_entities: list[dict]) -> str:
    if not known_entities:
        return "(none yet)"
    return "\n".join(
        f"  {e['name']}  [{e['type']}]" for e in sorted(known_entities, key=lambda e: e["name"])
    )


def build_prompt(
    script: Script,
    scene: Scene,
    known_facts: list[dict],
    known_entities: list[dict],
) -> str:
    """The user turn for one scene. The system turn is SYSTEM_PROMPT."""
    return f"""\
SCRIPT: {script.title or '(untitled)'}
REVISION NOTES: {script.title_page.get('notes', '(none)')}

SCENE {scene.scene_number} — {scene.heading}
Speaking parts in this scene: {', '.join(scene.characters) or '(none)'}

Lines {scene.start_line}–{scene.end_line}, numbered as they appear in the script.
Cite these numbers exactly; they are checked against the file.

{numbered(script, scene)}

ENTITIES ALREADY KNOWN (reuse these names rather than inventing new ones):
{_entity_block(known_entities)}

FACTS ALREADY ESTABLISHED IN EARLIER SCENES (reference these by handle):
{_established_block(known_facts)}

Return the facts scene {scene.scene_number} establishes for the first time, the \
knowledge state it settles, and its dependencies on the facts listed above."""
