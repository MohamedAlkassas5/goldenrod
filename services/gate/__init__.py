"""The Gate — call sheet in, ranked and cited findings out.

The second of the two agents (CLAUDE.md: "Two agents only"). It is the
orchestrator, and it does not free-associate: every step is a tool call against
ClickHouse through the MCP server, and every finding is assembled from rows that
came back.

    from services.gate import run_gate, load_call_sheet
    from services.common.mcp_client import ClickHouseMCP

    call_sheet = load_call_sheet(Path("data/fixtures/call-sheet.json"))
    with ClickHouseMCP() as ch:
        run = run_gate(call_sheet, ch)

    for finding in run.findings:      # already ranked by commitment state
        print(finding["commitment_state"], finding["scene"], finding["claim"])

See `services/gate/README.md` for the pipeline, the detection rules and the
reasons behind both.
"""

from services.gate.evidence import ScriptLines
from services.gate.findings import (
    build_findings,
    dismissal_id,
    finding_id,
    severity_for,
)
from services.gate.reader import GateReader
from services.gate.rules import Break, ChangedFact, DependencyEdge, detect
from services.gate.run import GateError, GateRun, Step, load_call_sheet, run_gate
from services.gate.scene_order import SceneOrder, scene_sort_key
from services.gate.writeback import mark_intentional, persist_findings

__all__ = [
    "Break",
    "ChangedFact",
    "DependencyEdge",
    "GateError",
    "GateReader",
    "GateRun",
    "SceneOrder",
    "ScriptLines",
    "Step",
    "build_findings",
    "detect",
    "dismissal_id",
    "finding_id",
    "load_call_sheet",
    "mark_intentional",
    "persist_findings",
    "run_gate",
    "scene_sort_key",
    "severity_for",
]
