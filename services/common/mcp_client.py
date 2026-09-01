"""ClickHouse access, exclusively through the ClickHouse MCP server.

CLAUDE.md rule 4, verbatim: "All ClickHouse access goes through the MCP server at
runtime. [...] No direct driver calls as a shortcut, not even temporarily."

So this module speaks MCP (JSON-RPC 2.0 over stdio) to the official
`mcp-clickhouse` server and calls its `run_query` tool. There is deliberately no
clickhouse-driver / clickhouse-connect import anywhere in `services/` — the only
process that holds a ClickHouse connection is the MCP server itself, launched
from the project's `.mcp.json`.

The client is written directly against the wire protocol rather than pulling in
the async `mcp` SDK: MCP over stdio is newline-delimited JSON-RPC, the loader has
no other reason to be async, and CLAUDE.md asks for a small implementation.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "2025-06-18"

REPO_ROOT = Path(__file__).resolve().parents[2]
MCP_CONFIG = REPO_ROOT / ".mcp.json"

_EXPAND = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class MCPError(RuntimeError):
    """The MCP server refused or failed a query."""


def _expand(value: str, env: dict[str, str]) -> str:
    """Expand ${VAR} and ${VAR:-default}, matching .mcp.json conventions."""
    return _EXPAND.sub(lambda m: env.get(m.group(1)) or (m.group(2) or ""), value)


def load_server_config(name: str = "clickhouse", config_path: Path | None = None) -> dict[str, Any]:
    """Read one server's launch config out of the project's .mcp.json."""
    path = config_path or MCP_CONFIG
    if not path.exists():
        raise MCPError(
            f"{path} not found. The loader launches ClickHouse access through the "
            f"MCP server declared there; it has no other way to reach the database."
        )
    servers = json.loads(path.read_text(encoding="utf-8")).get("mcpServers", {})
    if name not in servers:
        raise MCPError(f"No '{name}' server in {path}. Found: {sorted(servers)}")
    cfg = servers[name]
    env = dict(os.environ)
    resolved = {k: _expand(v, env) for k, v in (cfg.get("env") or {}).items()}
    return {
        "command": _expand(cfg["command"], env),
        "args": [_expand(a, env) for a in cfg.get("args", [])],
        "env": {**env, **resolved},
    }


class ClickHouseMCP:
    """Synchronous MCP stdio client scoped to the ClickHouse server's tools.

    Use as a context manager so the server subprocess is always reaped:

        with ClickHouseMCP() as ch:
            ch.run_query("SELECT 1")
    """

    def __init__(
        self,
        server: str = "clickhouse",
        config_path: Path | None = None,
        env: dict[str, str] | None = None,
    ):
        """`env` overlays the resolved launch environment for this client only.

        There is one caller and one reason: the database has to be created
        before anything can connect *to* it, so the schema loader opens a client
        against `default` first. Everything else uses the config as written.
        """
        self._cfg = load_server_config(server, config_path)
        if env:
            self._cfg["env"] = {**self._cfg["env"], **env}
        self._proc: subprocess.Popen | None = None
        self._id = 0
        # One MCP server, one stdio pipe, one request id counter. The CLIs are
        # single-threaded, but the API holds one long-lived client and serves
        # requests from a thread pool, so the pipe has to be serialised or two
        # callers read each other's replies.
        self._lock = threading.RLock()
        self.server_info: dict[str, Any] = {}
        self.tools: list[str] = []

    @property
    def is_alive(self) -> bool:
        """Whether the server subprocess is still up. Callers may reconnect."""
        return self._proc is not None and self._proc.poll() is None

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self) -> "ClickHouseMCP":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def connect(self) -> "ClickHouseMCP":
        self._proc = subprocess.Popen(
            [self._cfg["command"], *self._cfg["args"]],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._cfg["env"],
            text=True,
            encoding="utf-8",
            # The server's own logging goes to stderr in whatever the console
            # codepage is — on Windows that is not UTF-8, and a stray byte there
            # was killing the drain thread below. Diagnostics must never be able
            # to take down the only connection to the database.
            errors="replace",
            bufsize=1,
        )
        # Drain stderr so a chatty server can never fill the pipe and deadlock us.
        self._stderr: list[str] = []
        threading.Thread(
            target=lambda: [self._stderr.append(line) for line in self._proc.stderr],
            daemon=True,
        ).start()

        init = self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "goldenrod-loader", "version": "0.1.0"},
            },
        )
        self.server_info = init.get("result", {}).get("serverInfo", {})
        self._notify("notifications/initialized", {})
        listed = self._request("tools/list", {}).get("result", {}).get("tools", [])
        self.tools = [t["name"] for t in listed]
        if "run_query" not in self.tools:
            raise MCPError(
                f"MCP server exposes {self.tools}; 'run_query' is required. "
                f"Writes need CLICKHOUSE_ALLOW_WRITE_ACCESS=true in .mcp.json."
            )
        return self

    def close(self) -> None:
        if self._proc is None:
            return
        try:
            self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._proc.wait(timeout=5)
        except Exception:
            self._proc.kill()
        self._proc = None

    # -- protocol ----------------------------------------------------------
    def _send(self, payload: dict[str, Any]) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write(json.dumps(payload) + "\n")
        self._proc.stdin.flush()

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        with self._lock:
            self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            return self._exchange(method, params)

    def _exchange(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert self._proc and self._proc.stdout
        self._id += 1
        want = self._id
        self._send({"jsonrpc": "2.0", "id": want, "method": method, "params": params})
        while True:
            line = self._proc.stdout.readline()
            if not line:
                err = "".join(self._stderr[-20:])
                raise MCPError(f"MCP server closed the connection.\n{err}")
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # server logged non-protocol noise to stdout
            if msg.get("id") == want:
                return msg

    # -- the only database entry point -------------------------------------
    def run_query(self, sql: str) -> dict[str, Any]:
        """Execute one statement through the MCP `run_query` tool.

        Returns the decoded {"columns": [...], "rows": [...]} payload.
        Raises MCPError on a protocol error, a refused query (e.g. the
        destructive-operations guard) or a ClickHouse exception.
        """
        resp = self._request("tools/call", {"name": "run_query", "arguments": {"query": sql}})
        if "error" in resp:
            raise MCPError(f"{resp['error']}\n--- SQL ---\n{sql[:2000]}")
        result = resp.get("result", {})
        text = "".join(
            c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"
        )
        if result.get("isError"):
            raise MCPError(f"{text.strip()}\n--- SQL ---\n{sql[:2000]}")
        try:
            return json.loads(text) if text.strip() else {"columns": [], "rows": []}
        except json.JSONDecodeError:
            return {"columns": [], "rows": [], "raw": text}

    def rows(self, sql: str) -> list[dict[str, Any]]:
        """run_query, shaped as a list of dicts."""
        out = self.run_query(sql)
        cols = out.get("columns", [])
        return [dict(zip(cols, r)) for r in out.get("rows", [])]

    def scalar(self, sql: str) -> Any:
        out = self.run_query(sql)
        rows = out.get("rows", [])
        return rows[0][0] if rows and rows[0] else None
