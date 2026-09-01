"""Fountain parsing: screenplay text -> scenes, with line numbers that survive.

This half of the Extractor is deterministic. Sluglines, scene numbers, character
cues and dialogue are *structure* — a parser reads them exactly, and a model
would only add a way to get them wrong. The model is asked for one thing only:
the facts (see prompt.py). That split is why every citation Goldenrod emits can
be checked against the file.

LINE NUMBERS ARE THE CONTRACT. Every `line` recorded here is a 1-based index into
the original text, unchanged, so a finding's evidence can be opened in any editor
and read. Nothing in this module renumbers, reflows or normalises the source.

LOCKED PAGES ONLY. Goldenrod runs on locked scripts, so every slugline must carry
its scene number (`... - NIGHT #18#`). A slugline without one is refused rather
than silently given a made-up coordinate: scene_number is the identity the whole
fact-level diff rests on (db/clickhouse/schema.sql).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterator

# Contract enums, mirrored from contracts/graph.schema.json.
INT_EXT = ("INT", "EXT", "INT/EXT")
DAY_NIGHT = ("DAY", "NIGHT", "DUSK", "DAWN", "CONTINUOUS")

_HEADING = re.compile(
    r"^(?P<prefix>INT\.?/EXT\.?|I/E\.?|INT\.?|EXT\.?|EST\.?)(?=[\s.])[\s.]*(?P<rest>.*)$"
)
_SCENE_NUMBER = re.compile(r"\s*#(?P<number>[0-9A-Za-z]+)#\s*$")
_PARENTHETICAL = re.compile(r"^\(.*\)$")
_TRANSITION = re.compile(r"^(?:[A-Z][A-Z0-9 '\-]*TO:|FADE (?:IN|OUT)[:.]?|CUT TO BLACK\.?)$")


class FountainError(ValueError):
    """The screenplay could not be parsed as a locked, numbered script."""


@dataclass(frozen=True)
class Element:
    """One addressable piece of a scene, at a real line number."""

    type: str  # heading | action | character | parenthetical | dialogue | transition
    line: int
    text: str
    character: str = ""  # set on parenthetical/dialogue: whose speech this is


@dataclass
class Scene:
    scene_number: str
    scene_id: str
    int_ext: str
    day_night: str
    location: str
    heading: str
    start_line: int
    end_line: int
    elements: list[Element] = field(default_factory=list)

    @property
    def characters(self) -> list[str]:
        """Cue names, in order of first appearance. Speaking parts only."""
        seen: list[str] = []
        for element in self.elements:
            if element.type == "character" and element.text not in seen:
                seen.append(element.text)
        return seen

    def dialogue_of(self, character: str) -> list[Element]:
        return [
            e for e in self.elements if e.type == "dialogue" and e.character == character
        ]


@dataclass
class Script:
    title_page: dict[str, str]
    scenes: list[Scene]
    lines: list[str]

    @property
    def title(self) -> str:
        return self.title_page.get("title", "")

    def scene(self, scene_number: str) -> Scene:
        for s in self.scenes:
            if s.scene_number == scene_number:
                return s
        raise KeyError(
            f"no scene {scene_number!r}; have {[s.scene_number for s in self.scenes]}"
        )

    def line_text(self, line: int) -> str:
        """The source line, 1-based. Raises rather than returning something wrong."""
        if not 1 <= line <= len(self.lines):
            raise IndexError(f"line {line} is outside the script (1..{len(self.lines)})")
        return self.lines[line - 1]


# --- sluglines -------------------------------------------------------------
def _normalise_prefix(prefix: str) -> str:
    upper = prefix.upper().replace(".", "")
    if upper in ("INT/EXT", "I/E"):
        return "INT/EXT"
    if upper == "EST":
        return "EXT"  # establishing shots are exteriors; the contract has no EST
    return upper


def parse_heading(text: str) -> tuple[str, str, str, str] | None:
    """(scene_number, int_ext, day_night, location) for a slugline, else None."""
    match = _HEADING.match(text.strip())
    if not match:
        return None
    rest = match.group("rest").strip()

    number_match = _SCENE_NUMBER.search(rest)
    if not number_match:
        return None
    scene_number = number_match.group("number")
    rest = rest[: number_match.start()].strip()

    day_night = ""
    parts = [p.strip() for p in re.split(r"\s+[-–—]\s+", rest) if p.strip()]
    if parts and parts[-1].upper() in DAY_NIGHT:
        day_night = parts.pop().upper()
    location = " - ".join(parts)

    return scene_number, _normalise_prefix(match.group("prefix")), day_night, location


def _looks_like_slugline(text: str) -> bool:
    """A slugline shape, whether or not it carries a scene number."""
    return bool(_HEADING.match(text.strip()))


# --- cues ------------------------------------------------------------------
def _is_character_cue(lines: list[str], index: int) -> bool:
    """Fountain's rule: uppercase, blank line above, content directly below.

    The "content directly below" half is what keeps `INSERT - THE LETTER` and
    `BACK TO SCENE` out of the cast list — both are followed by a blank line.
    """
    text = lines[index].strip()
    if not text:
        return False
    if index > 0 and lines[index - 1].strip():
        return False
    if index + 1 >= len(lines) or not lines[index + 1].strip():
        return False
    if _TRANSITION.match(text) or text.endswith("TO:"):
        return False
    core = re.sub(r"\(.*?\)", "", text).replace("^", "").strip()
    if not core or not re.search(r"[A-Z]", core):
        return False
    return core == core.upper()


def cue_name(text: str) -> str:
    """`MRS. WADIDA (CONT'D)` -> `MRS. WADIDA`. The speaker, without the modifier."""
    return re.sub(r"\(.*?\)", "", text).replace("^", "").strip()


# --- the parser ------------------------------------------------------------
def _title_page(lines: list[str]) -> tuple[dict[str, str], int]:
    """Key: value pairs above the `====` rule. Returns (fields, body start index)."""
    fields: dict[str, str] = {}
    for index, raw in enumerate(lines):
        text = raw.strip()
        if text.startswith("==="):
            return fields, index + 1
        if _looks_like_slugline(text):
            return fields, index  # no title page at all
        if ":" in text:
            key, _, value = text.partition(":")
            fields[key.strip().lower()] = value.strip()
    return fields, len(lines)


def _scene_spans(
    lines: list[str], start: int
) -> Iterator[tuple[int, tuple[str, str, str, str]]]:
    for index in range(start, len(lines)):
        heading = parse_heading(lines[index])
        if heading:
            yield index, heading
        elif _looks_like_slugline(lines[index]):
            raise FountainError(
                f"line {index + 1}: slugline without a scene number: "
                f"{lines[index].strip()!r}\nGoldenrod runs on locked pages only — "
                f"scene_number is the coordinate the whole fact-level diff rests on."
            )


def parse_fountain(text: str) -> Script:
    """Parse a locked, scene-numbered Fountain screenplay."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    title_page, body_start = _title_page(lines)

    spans = list(_scene_spans(lines, body_start))
    if not spans:
        raise FountainError(
            "no numbered sluglines found. Expected headings like "
            "'INT. FLAT, KITCHEN - NIGHT #22#'."
        )

    scenes: list[Scene] = []
    for position, (index, (number, int_ext, day_night, location)) in enumerate(spans):
        end = spans[position + 1][0] - 1 if position + 1 < len(spans) else len(lines) - 1
        while end > index and not lines[end].strip():
            end -= 1
        scenes.append(
            Scene(
                scene_number=number,
                scene_id=f"sc{number}",
                int_ext=int_ext,
                day_night=day_night,
                location=location,
                heading=lines[index].strip(),
                start_line=index + 1,
                end_line=end + 1,
                elements=_elements(lines, index, end),
            )
        )

    numbers = [s.scene_number for s in scenes]
    duplicates = sorted({n for n in numbers if numbers.count(n) > 1})
    if duplicates:
        raise FountainError(
            f"duplicate scene numbers {duplicates}. Scene number is the stable "
            f"coordinate across revisions; it cannot repeat."
        )

    return Script(title_page=title_page, scenes=scenes, lines=lines)


def _elements(lines: list[str], heading_index: int, end_index: int) -> list[Element]:
    elements = [Element("heading", heading_index + 1, lines[heading_index].strip())]
    speaker = ""
    for index in range(heading_index + 1, end_index + 1):
        text = lines[index].strip()
        if not text:
            speaker = ""
            continue
        line = index + 1
        if _is_character_cue(lines, index):
            speaker = cue_name(text)
            elements.append(Element("character", line, speaker))
        elif speaker and _PARENTHETICAL.match(text):
            elements.append(Element("parenthetical", line, text, speaker))
        elif speaker:
            elements.append(Element("dialogue", line, text, speaker))
        elif _TRANSITION.match(text):
            elements.append(Element("transition", line, text))
        else:
            elements.append(Element("action", line, text))
    return elements


def numbered(script: Script, scene: Scene) -> str:
    """The scene as `<line>| <text>`, which is what the model is shown.

    The model cites line numbers back to us and we check them against the file,
    so the numbering it sees has to be the real one.
    """
    return "\n".join(
        f"{n}| {script.lines[n - 1].rstrip()}"
        for n in range(scene.start_line, scene.end_line + 1)
    )
