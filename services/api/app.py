"""HTTP entry point: the call sheet, with findings attached.

This is the surface the interface talks to. It is deliberately thin — it holds
the MCP connection, reads the call sheet, and hands the Gate's own output
through. No detection, no ranking and no SQL live here; those are in
`services/gate`, which is where they can be tested without a web server.

WHY THE CHECK FIRES ON ITS OWN
------------------------------
`GET /api/run/stream` runs the check as the page loads and streams each pipeline
step as it completes. Nobody presses anything — the call sheet being issued is
the trigger (SPEC §3), and a check somebody has to remember to run is a check
that does not get run. The steps are real: `run_gate` calls back as each stage
finishes, so what the interface draws is the pipeline, not an animation of it.

ONE LONG-LIVED MCP CONNECTION
-----------------------------
Spawning the MCP server per request would put a second of subprocess startup in
front of every page. So the app holds one client for its lifetime, reconnecting
if the server dies. `ClickHouseMCP` serialises its own pipe, so concurrent
requests are safe.

EVERY ENDPOINT IS SCOPED, AND SCOPING IS NOT DONE HERE
------------------------------------------------------
An unreleased script is access-controlled, so every route below resolves the
identity its platform proved (services/api/auth.py) and passes the result
through the pure scoping functions in services/common/access.py. This module
decides *whether* a route needs a permission; it never decides what a role may
see. Both halves are enforced server-side — the interface's identity switcher
changes a header, not a filter, and asking for another department's finding by
id returns nothing extra.
"""

from __future__ import annotations

import json
import os
import queue
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Iterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from services.api.auth import (
    AccessDenied,
    Directory,
    Viewer,
    identity_chooser_enabled,
    subject_from_headers,
)
from services.api.browse import Browser
from services.common.access import Role, scope_graph, scope_run, scope_step
from services.common.mcp_client import ClickHouseMCP, MCPError
from services.gate import GateError, ScriptLines, run_gate
from services.gate.writeback import mark_intentional, persist_findings

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = REPO_ROOT / "web"
DEFAULT_CALL_SHEET = REPO_ROOT / "data" / "fixtures" / "call-sheet.json"
DEFAULT_PAGES = REPO_ROOT / "data" / "fixtures" / "script-v2.fountain"


class Settings:
    """What this instance is checking. All of it overridable by environment.

    The environment is read on every access rather than snapshotted in
    __init__. `services/api/__init__.py` imports this module, so the app object
    is built the moment anything imports the package — before `__main__` has had
    a chance to apply its flags. Reading late is what makes those flags work,
    and it is what the docstring above has always promised.
    """

    @property
    def call_sheet_path(self) -> Path:
        return Path(os.environ.get("GOLDENROD_CALL_SHEET") or DEFAULT_CALL_SHEET)

    @property
    def pages_path(self) -> Path | None:
        pages = os.environ.get("GOLDENROD_PAGES", str(DEFAULT_PAGES))
        return Path(pages) if pages else None

    @property
    def production_override(self) -> str:
        """Overrides the call sheet's own production_id.

        Lets one checkout run against a test production without editing the
        fixture. Empty means "use whatever the call sheet says".
        """
        return os.environ.get("PRODUCTION_ID", "").strip()

    def call_sheet(self) -> dict[str, Any]:
        from services.gate.run import validate_call_sheet

        try:
            sheet = json.loads(self.call_sheet_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GateError(f"could not read {self.call_sheet_path}: {exc}") from exc
        if self.production_override:
            sheet["production_id"] = self.production_override
        validate_call_sheet(sheet)
        return sheet

    def pages(self) -> ScriptLines | None:
        if self.pages_path and self.pages_path.exists():
            return ScriptLines.from_path(self.pages_path)
        return None


class Intentional(BaseModel):
    """A human accepting a deviation.

    The reason is mandatory and the signature is not here at all: who marked it
    is the identity the platform proved, never a name the client typed. A
    dismissal of an unreleased-script finding that anyone can sign as anyone is
    not an audit trail.
    """

    reason: str = Field(min_length=1)
    cause_tag: str = "taste"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        # Always reap the MCP subprocess. It holds the only ClickHouse
        # connection in the system; leaking one leaks a database session.
        if app.state.mcp is not None:
            app.state.mcp.close()
            app.state.mcp = None

    app = FastAPI(
        title="Goldenrod",
        description="Pre-commitment check for film and TV production.",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.mcp: ClickHouseMCP | None = None
    app.state.mcp_lock = threading.Lock()

    def clickhouse() -> ClickHouseMCP:
        """The shared MCP client, connected on first use and after a drop."""
        with app.state.mcp_lock:
            client = app.state.mcp
            if client is not None and client.is_alive:
                return client
            if client is not None:
                client.close()
            app.state.mcp = ClickHouseMCP().connect()
            return app.state.mcp

    app.state.clickhouse = clickhouse

    def check(**kwargs):
        return run_gate(
            settings.call_sheet(), clickhouse(), pages=settings.pages(), **kwargs
        )

    # -- who is asking -------------------------------------------------------
    def directory() -> Directory:
        return Directory(clickhouse(), settings.call_sheet()["production_id"])

    def identify(request: Request) -> tuple[Viewer, Directory]:
        """Resolve the caller, or refuse. Every scoped route starts here.

        `AccessDenied` already carries the right status — 401 when nobody was
        identified, 403 when somebody was and holds no grant — and the refusal
        has already been written to the audit trail by the time this raises.
        """
        book = directory()
        try:
            return book.viewer(subject_from_headers(request.headers)), book
        except AccessDenied as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        except (GateError, MCPError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    def require(viewer: Viewer, book: Directory, permission: str) -> None:
        """Refuse, and record the refusal, when a role lacks a permission."""
        if getattr(viewer.role, permission, False):
            return
        book.log(
            subject=viewer.subject, role=viewer.role.name, action="denied",
            granted=False, detail=f"{viewer.role.name} lacks {permission}",
        )
        raise HTTPException(
            status_code=403,
            detail=f"{viewer.role.title} is not granted {permission} on this production.",
        )

    # -- access --------------------------------------------------------------
    @app.get("/api/access/identities")
    def identities() -> dict[str, Any]:
        """The roster, for the interface's identity chooser.

        This route is the local stand-in for the platform's account picker and
        exists only while GOLDENROD_IDENTITY_CHOOSER is on. Behind Cloud IAP it
        is switched off and the identity arrives on the request instead — a
        deployment setting, not a code change.
        """
        if not identity_chooser_enabled():
            raise HTTPException(
                status_code=404,
                detail="the identity chooser is off; identity comes from the platform.",
            )
        try:
            return {"chooser": True, "identities": directory().identities()}
        except (GateError, MCPError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/access/me")
    def whoami(request: Request) -> dict[str, Any]:
        viewer, _ = identify(request)
        return viewer.as_dict()

    @app.get("/api/access/log")
    def access_log(request: Request, limit: int = 100) -> dict[str, Any]:
        """The audit trail. Oversight is the production office's, not a department's."""
        viewer, book = identify(request)
        require(viewer, book, "read_ledger")
        return {"trail": book.trail(min(max(limit, 1), 500))}

    # -- the day ------------------------------------------------------------
    @app.get("/api/call-sheet")
    def read_call_sheet(request: Request) -> dict[str, Any]:
        """The day itself, unscoped past the sign-in.

        A call sheet is the one production document that genuinely does go to
        everybody — that is what it is for. What it does not carry is pages, and
        that is the line this system draws too.
        """
        identify(request)
        return settings.call_sheet()

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        """An ops probe, so it carries no identity and returns no production data."""
        try:
            ch = clickhouse()
            return {
                "ok": True,
                "mcp": {"server": ch.server_info, "tools": ch.tools},
                "clickhouse": ch.scalar("SELECT version()"),
                "call_sheet": str(settings.call_sheet_path),
                "pages": str(settings.pages_path) if settings.pages_path else None,
                "access": {
                    "grants": len(directory().roster()),
                    "identity_chooser": identity_chooser_enabled(),
                },
            }
        except (MCPError, GateError) as exc:
            return {"ok": False, "error": str(exc)}

    # -- the check ----------------------------------------------------------
    def _record(book: Directory, viewer: Viewer, scoped: dict[str, Any]) -> None:
        book.log(
            subject=viewer.subject,
            role=viewer.role.name,
            action="check",
            granted=True,
            released=len(scoped.get("findings", [])),
            withheld=int(scoped.get("access", {}).get("withheld", 0)),
            detail=f"{scoped.get('shoot_date', '')} {scoped.get('revision_id', '')}",
        )

    @app.get("/api/run")
    def run(request: Request) -> dict[str, Any]:
        """The whole check, in one response. The stream is what the UI uses."""
        viewer, book = identify(request)
        try:
            scoped = scope_run(check().as_dict(), viewer.role)
        except (GateError, MCPError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        _record(book, viewer, scoped)
        return scoped

    @app.get("/api/run/stream")
    def run_stream(request: Request) -> StreamingResponse:
        """The check, streamed: one event per pipeline step, then the result.

        The Gate is synchronous, so it runs on a worker thread and pushes each
        completed step into a queue. Nothing is simulated — a step appears when
        that stage actually finished, and its `ms` is what it actually took.

        The identity is resolved before the response begins, so a refusal is an
        HTTP status rather than an error event inside a 200.
        """
        viewer, book = identify(request)
        return StreamingResponse(
            _stream(check, viewer.role, lambda s: _record(book, viewer, s)),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    # -- the write-back -----------------------------------------------------
    @app.post("/api/findings/{finding_id}/intentional")
    def accept(finding_id: str, body: Intentional, request: Request) -> dict[str, Any]:
        """Mark a finding intentional, then re-run the identical check.

        The re-run is the point, not a convenience: SPEC §8 asks that marking
        something intentional produces a demonstrably different result, and the
        only honest way to show that is to run the same check again.

        Accepting a deviation is an act of the production office, so it needs
        `write_ledger` — a department head can see the break they own and cannot
        sign it off, and neither can a producer. The signature written to the
        ledger is the proved identity, not anything the request supplied.
        """
        viewer, book = identify(request)
        require(viewer, book, "write_ledger")
        try:
            ch = clickhouse()
            before = check()
            target = next(
                (f for f in before.findings if f["finding_id"] == finding_id), None
            )
            if target is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"no open finding {finding_id} in the current check",
                )
            marked = mark_intentional(
                ch,
                before.production_id,
                target,
                body.reason,
                viewer.subject,
                revision_id=before.revision_id,
                cause_tag=body.cause_tag,
            )
            book.log(
                subject=viewer.subject, role=viewer.role.name, action="ledger_write",
                granted=True, detail=f"scene {target['scene']} marked intentional",
            )
            after = check()
            return {"marked": marked, "run": scope_run(after.as_dict(), viewer.role)}
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except (GateError, MCPError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/run/persist")
    def persist(request: Request) -> dict[str, Any]:
        viewer, book = identify(request)
        require(viewer, book, "write_ledger")
        try:
            run_result = check()
            written = persist_findings(clickhouse(), run_result)
            return {"run_id": run_result.run_id, "written": written}
        except (GateError, MCPError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    # -- what the system knows ----------------------------------------------
    @app.get("/api/graph")
    def graph(request: Request, revision: str = "") -> dict[str, Any]:
        """Scenes, facts, knowledge state and the ledger with its reasons.

        Scoped: the pages in structured form and the office's reasons are
        withheld from a role granted neither, and the response says which
        surfaces were withheld rather than returning a quietly empty list.
        """
        viewer, book = identify(request)
        try:
            sheet = settings.call_sheet()
            browser = Browser(clickhouse(), sheet["production_id"])
            scoped = scope_graph(
                browser.graph(revision or sheet["revision_id"]), viewer.role
            )
        except (GateError, MCPError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        book.log(
            subject=viewer.subject, role=viewer.role.name, action="graph", granted=True,
            detail="withheld: "
            + (", ".join(scoped["access"]["withheld_surfaces"]) or "nothing"),
        )
        return scoped

    # -- the interface ------------------------------------------------------
    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(WEB_DIR / "index.html")

    return app


def _stream(check, role: Role, on_finish=None) -> Iterator[str]:
    """Server-sent events for one run: `step` events, then `done` or `error`.

    Every step is scoped on its way out, not only the final result. A step's
    `sample` is rows the query returned, and those rows carry statements and
    quoted lines — a viewer with no page access must not receive them at 0:45
    and then be told at 1:15 that they cannot see the pages.
    """
    events: queue.Queue = queue.Queue()
    DONE = object()

    def worker() -> None:
        try:
            result = check(
                on_step=lambda step: events.put(
                    ("step", scope_step(step.as_dict(), role))
                )
            )
            scoped = scope_run(result.as_dict(), role)
            if on_finish is not None:
                on_finish(scoped)
            events.put(("done", scoped))
        except Exception as exc:  # reported to the page, not swallowed
            events.put(("error", {"detail": str(exc)}))
        finally:
            events.put(DONE)

    threading.Thread(target=worker, daemon=True).start()
    while True:
        item = events.get()
        if item is DONE:
            return
        name, payload = item
        yield f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


app = create_app()
