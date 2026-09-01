"""CLI: screenplay in, graph JSON out.

    python -m services.extractor data/fixtures/script-v2.fountain \\
        --production fayoum --revision goldenrod-2026-08-29 \\
        -o data/fixtures/graph-v2.json

    python -m services.extractor script.fountain --structure-only   # no model call

The output is written to disk rather than straight into ClickHouse on purpose. A
revision is extracted once and loaded many times, the graph is the artefact a
person can read and diff, and `python -m services.loader` already owns the write
path through the MCP server. Chain them:

    python -m services.extractor script.fountain -o graph.json --production fayoum \\
        --revision goldenrod-2026-08-29
    python -m services.loader graph.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from services.extractor.backend import StructureOnlyBackend
from services.extractor.extract import ExtractionError, extract_graph
from services.extractor.fountain import FountainError, parse_fountain
from services.extractor.gemini import GeminiBackend, GeminiError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m services.extractor",
        description="Extract a film graph from a locked, scene-numbered screenplay.",
    )
    parser.add_argument("script", type=Path, help="Fountain screenplay")
    parser.add_argument("--production", required=True, help="production_id")
    parser.add_argument(
        "--revision",
        help="revision_id, e.g. goldenrod-2026-08-29. "
        "Defaults to the script's title-page draft date.",
    )
    parser.add_argument("-o", "--out", type=Path, help="write graph JSON here")
    parser.add_argument("--model", help="overrides GEMINI_MODEL")
    parser.add_argument(
        "--structure-only",
        action="store_true",
        help="parse scenes and entities without calling the model",
    )
    parser.add_argument("--quiet", action="store_true", help="report only, no per-scene lines")
    args = parser.parse_args(argv)

    try:
        script = parse_fountain(args.script.read_text(encoding="utf-8"))
    except (OSError, FountainError) as exc:
        print(f"could not parse {args.script}: {exc}", file=sys.stderr)
        return 2

    revision = args.revision or script.title_page.get("draft date", "")
    if not revision:
        print(
            "no --revision given and the title page has no 'Draft date:'. "
            "A revision id is required: it is what the fact-level diff runs between.",
            file=sys.stderr,
        )
        return 2

    backend = StructureOnlyBackend() if args.structure_only else GeminiBackend(args.model)
    print(f"{args.script.name}: {len(script.scenes)} scenes, backend={backend.name}")

    def progress(scene, report):
        if not args.quiet:
            print(
                f"  scene {scene.scene_number:<4} "
                f"facts {report.facts:>3}  knowledge {report.knowledge_state:>3}  "
                f"deps {report.dependencies:>3}  dropped {len(report.dropped):>3}"
            )

    try:
        graph, report = extract_graph(
            script, args.production, revision, backend, on_scene=progress
        )
    except (ExtractionError, GeminiError) as exc:
        print(f"\nEXTRACTION FAILED\n{exc}", file=sys.stderr)
        return 1

    print()
    print(report.summary())

    if args.out:
        args.out.write_text(json.dumps(graph, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nwrote {args.out}")
    else:
        print("\nno --out given; nothing written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
