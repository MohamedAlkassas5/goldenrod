"""CLI: fire the Gate on a call sheet.

    python -m services.gate data/fixtures/call-sheet.json \\
        --pages data/fixtures/script-v2.fountain

    python -m services.gate call-sheet.json --json -o run.json    # for the UI
    python -m services.gate call-sheet.json --write               # persist the run

Marking a finding intentional, which is the write-back the demo shows at
2:00–2:30 — after it, the identical check produces a different result:

    python -m services.gate call-sheet.json --mark <finding_id> \\
        --reason "Network note: the reveal holds to the grove" \\
        --by script.coordinator@fayoum

`--pages` is optional and worth giving. The graph stores the line number a fact
was established on but not the words on that line, so without the pages a
finding cites the dependency's own verified quote and simply omits the line that
caused the break. It is never filled in from the model's paraphrase.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from services.common.mcp_client import ClickHouseMCP, MCPError
from services.gate.evidence import ScriptLines
from services.gate.run import GateError, load_call_sheet, run_gate
from services.gate.writeback import mark_intentional, persist_findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m services.gate",
        description="Check tomorrow's call sheet against the current revision.",
    )
    parser.add_argument("call_sheet", type=Path, help="contracts/call-sheet.schema.json")
    parser.add_argument(
        "--pages", type=Path, help="the revision's Fountain file, for citation quotes"
    )
    parser.add_argument("--json", action="store_true", help="emit the run as JSON")
    parser.add_argument("-o", "--out", type=Path, help="write the run JSON here")
    parser.add_argument(
        "--write", action="store_true", help="persist the findings to ClickHouse"
    )
    parser.add_argument("--mark", help="finding_id to mark as an intentional deviation")
    parser.add_argument("--reason", default="", help="why it is intentional. Mandatory")
    parser.add_argument("--by", default="", help="who is marking it. Every write is attributed")
    args = parser.parse_args(argv)

    try:
        call_sheet = load_call_sheet(args.call_sheet)
        pages = ScriptLines.from_path(args.pages) if args.pages else None

        with ClickHouseMCP() as ch:
            run = run_gate(call_sheet, ch, pages=pages)

            if args.mark:
                target = next(
                    (f for f in run.findings if f["finding_id"] == args.mark), None
                )
                if target is None:
                    print(
                        f"no open finding {args.mark!r} in this run. Open findings:\n"
                        + "\n".join(
                            f"  {f['finding_id']}  scene {f['scene']}" for f in run.findings
                        ),
                        file=sys.stderr,
                    )
                    return 2
                marked = mark_intentional(
                    ch,
                    run.production_id,
                    target,
                    args.reason,
                    args.by,
                    revision_id=run.revision_id,
                )
                print(
                    f"scene {marked['scene']} marked intentional by {marked['marked_by']}"
                    + (" (already on the ledger)" if marked["already_marked"] else "")
                )
                print("re-running the identical check...\n")
                run = run_gate(call_sheet, ch, pages=pages)

            if args.write:
                written = persist_findings(ch, run)
                print(f"persisted {written} findings as run {run.run_id}\n")

        if args.json or args.out:
            payload = json.dumps(run.as_dict(), indent=2, ensure_ascii=False)
            if args.out:
                args.out.write_text(payload, encoding="utf-8")
                print(f"wrote {args.out}")
            if args.json:
                print(payload)
        else:
            print(run.summary())
        return 0

    except (GateError, MCPError, ValueError) as exc:
        print(f"\nGATE FAILED\n{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
