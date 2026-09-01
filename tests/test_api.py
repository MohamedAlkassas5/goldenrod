"""The HTTP surface: the call sheet, the streamed check, and the write-back.

The API is deliberately thin, so these tests check the things a thin layer can
still get wrong — that the check fires without being asked, that the stream
carries real steps in order, that the query AND its rows reach the page, that a
dismissal is refused without a reason or a name, and that a production with no
graph loaded is reported as a failure rather than as an all-clear.

Detection, ranking and evidence are tested in tests/test_gate.py, against the
answer key. Nothing is re-asserted here.

Writes go to their own production, never to the one scored against the answer
key: the ledger is append-only, so a leaked dismissal would be permanent.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="the API needs: pip install -e \".[api]\"")

from fastapi.testclient import TestClient  # noqa: E402

from services.api import Settings, create_app  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "data" / "fixtures"

READ_PRODUCTION = "test_gate"          # loaded by tests/test_gate.py, read-only here
WRITE_PRODUCTION = "test_api_writeback"

# Every route is behind the access roster now, so these tests have to sign in
# like anything else. The scoping itself is tested in tests/test_access.py; here
# the office identity is just what makes the rest of the surface reachable.
OFFICE = {"X-Goldenrod-Subject": "script.coordinator@fayoum"}

pytestmark = pytest.mark.mcp


@pytest.fixture(scope="module")
def loaded():
    """Both revisions and the ledger, for the productions these tests use."""
    from services.common.mcp_client import ClickHouseMCP, MCPError
    from tests.demo_graph import load_production

    try:
        client = ClickHouseMCP().connect()
        client.run_query("SELECT 1")
    except (MCPError, OSError, FileNotFoundError) as exc:
        pytest.skip(f"no ClickHouse reachable through MCP: {exc}")
    load_production(client, READ_PRODUCTION)
    load_production(client, WRITE_PRODUCTION)
    yield client
    client.close()


@pytest.fixture
def client(loaded, monkeypatch):
    monkeypatch.setenv("PRODUCTION_ID", READ_PRODUCTION)
    monkeypatch.setenv("GOLDENROD_CALL_SHEET", str(FIXTURES / "call-sheet.json"))
    monkeypatch.setenv("GOLDENROD_PAGES", str(FIXTURES / "script-v2.fountain"))
    with TestClient(create_app(), headers=OFFICE) as c:
        yield c


# --- the day ---------------------------------------------------------------
def test_the_call_sheet_is_served_and_valid(client):
    sheet = client.get("/api/call-sheet").json()
    assert sheet["production_id"] == READ_PRODUCTION
    assert [s["scene_number"] for s in sheet["scenes"]] == ["18", "24", "26", "27"]


def test_settings_read_the_environment_when_asked_not_at_import(monkeypatch):
    """The package builds an app on import, before __main__ applies its flags.
    Reading late is what makes --production work at all."""
    settings = Settings()
    monkeypatch.setenv("PRODUCTION_ID", "somewhere_else")
    assert settings.production_override == "somewhere_else"
    monkeypatch.delenv("PRODUCTION_ID")
    assert settings.production_override == ""


def test_health_reports_the_mcp_server_it_is_talking_through(client):
    health = client.get("/api/health").json()
    assert health["ok"] is True
    assert "run_query" in health["mcp"]["tools"]
    assert health["mcp"]["server"]["name"]


# --- the check -------------------------------------------------------------
def test_the_check_runs_and_returns_ranked_findings(client):
    run = client.get("/api/run").json()
    assert [f["scene"] for f in run["findings"]] == ["22", "24", "33"]
    assert [f["commitment_state"] for f in run["findings"]] == ["shot", "cast", "planned"]


def test_the_stream_carries_every_step_in_order_then_the_result(client):
    """SPEC §7: the pipeline renders as it completes. Nobody presses anything."""
    events = []
    with client.stream("GET", "/api/run/stream") as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        name = None
        for line in response.iter_lines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: ") and name:
                events.append((name, json.loads(line[6:])))

    assert [n for n, _ in events] == ["step"] * 6 + ["done"]
    assert [p["name"] for n, p in events if n == "step"] == [
        "parse_day", "draft_diff", "traverse", "ledger", "commitment", "rank"
    ]
    assert events[-1][1]["findings"][0]["scene"] == "22"


def test_each_step_carries_its_query_and_the_rows_it_returned(client):
    """A query shown without its result is a screenshot of intent, not of work."""
    steps = {s["name"]: s for s in client.get("/api/run").json()["steps"]}

    ledger = steps["ledger"]
    assert "commitmentRank(" in ledger["sql"], "the ranking UDF must be on screen"
    assert "argMax" in ledger["sql"] and "GROUP BY" in ledger["sql"]
    assert ledger["sample"], "the ledger frame must show rows, not just SQL"
    assert "commitment_state" in ledger["sample"][0]

    assert steps["parse_day"]["sql"] == "", "parsing the day reads no database"
    assert steps["draft_diff"]["sample"][0]["change"] == "RELOCATED"


def test_a_production_with_no_graph_is_a_failure_not_an_all_clear(loaded, client, monkeypatch):
    """Silence has to mean silence, over HTTP as much as on the command line.

    The roster is seeded for the empty production first, so what this asserts is
    the missing graph and not the missing grant — access refuses earlier, and a
    403 here would prove nothing about the check.
    """
    import copy

    from services.loader.seed import load_seed_files, seed_ledger

    empty = "test_api_nothing_loaded"
    roster = copy.deepcopy(load_seed_files(FIXTURES))
    roster["production_id"] = empty
    roster.pop("decisions", None)
    roster.pop("commitments", None)
    seed_ledger(roster, loaded)

    monkeypatch.setenv("PRODUCTION_ID", empty)
    response = client.get("/api/run")
    assert response.status_code == 409
    assert "no scenes on file" in response.json()["detail"]


# --- what the office has logged -------------------------------------------
def test_the_graph_view_serves_the_ledger_with_its_reasons(client):
    graph = client.get("/api/graph").json()
    assert graph["scenes"] and graph["facts"] and graph["knowledge_state"]
    assert all(d["reason"].strip() for d in graph["decisions"]), (
        "a decision with no reason cannot support a finding and should not exist"
    )
    assert any(c["commitment_state"] if "commitment_state" in c else c["state"] == "shot"
               for c in graph["commitments"])


# --- the write-back --------------------------------------------------------
def _writeback_client(monkeypatch) -> TestClient:
    monkeypatch.setenv("PRODUCTION_ID", WRITE_PRODUCTION)
    monkeypatch.setenv("GOLDENROD_CALL_SHEET", str(FIXTURES / "call-sheet.json"))
    monkeypatch.setenv("GOLDENROD_PAGES", str(FIXTURES / "script-v2.fountain"))
    return TestClient(create_app(), headers=OFFICE)


def test_an_unexplained_dismissal_is_refused(loaded, monkeypatch):
    """Every ledger write carries a reason. This is an unreleased script."""
    with _writeback_client(monkeypatch) as client:
        finding = client.get("/api/run").json()["findings"][0]
        response = client.post(
            f"/api/findings/{finding['finding_id']}/intentional", json={"reason": ""}
        )
        assert response.status_code == 422


def test_marking_an_unknown_finding_is_a_404(loaded, monkeypatch):
    with _writeback_client(monkeypatch) as client:
        response = client.post(
            "/api/findings/not-a-finding/intentional",
            json={"reason": "because"},
        )
        assert response.status_code == 404


def test_marking_intentional_silences_it_and_the_rerun_differs(loaded, monkeypatch):
    """SPEC §8: the re-run after marking must produce a different result.

    Idempotent across runs: the dismissal id is derived from the finding id, so a
    second execution of this test finds scene 24 already silenced and asserts the
    same end state.
    """
    with _writeback_client(monkeypatch) as client:
        before = client.get("/api/run").json()
        open_scenes = {f["scene"] for f in before["findings"]}
        target = next(
            (f for f in before["findings"] if f["scene"] == "24"),
            next((f for f in before["dismissed"] if f["scene"] == "24"), None),
        )
        assert target, "scene 24 should be in the check, open or already silenced"

        response = client.post(
            f"/api/findings/{target['finding_id']}/intentional",
            json={
                "reason": "Rana confronts Tarek on suspicion, not on the letter.",
                # Deliberately supplied, and deliberately ignored: the signature
                # on the ledger is the identity the platform proved.
                "marked_by": "somebody.else@nowhere",
            },
        )
        if "24" not in open_scenes:  # already accepted by an earlier run
            assert response.status_code == 404
            after = before
        else:
            assert response.status_code == 200
            after = response.json()["run"]
            assert after != before

        assert "24" not in {f["scene"] for f in after["findings"]}
        silenced = next(f for f in after["dismissed"] if f["scene"] == "24")
        assert silenced["dismissed"]["marked_by"] == "script.coordinator@fayoum"
        assert silenced["dismissed"]["intentional"] is True


def test_the_related_finding_cites_the_accepted_deviation(loaded, monkeypatch):
    """SPEC §7, 2:00–2:30: a related flag resolves differently, on screen.

    Runs after the test above has accepted scene 24, so scene 22 — the same
    changed fact, a different scene — should now carry it as evidence.
    """
    with _writeback_client(monkeypatch) as client:
        run = client.get("/api/run").json()
        scene22 = next(f for f in run["findings"] if f["scene"] == "22")
        assert "scene 24" in scene22["suggested_action"]
        reasons = [e["reason"] for e in scene22["evidence"] if e["type"] == "decision"]
        assert any("suspicion" in r for r in reasons), (
            "the accepted deviation should be cited, not merely remembered"
        )


def test_persisting_a_run_writes_every_finding_under_one_run_id(client):
    payload = client.post("/api/run/persist").json()
    assert payload["written"] == 3
    assert payload["run_id"]


# --- the interface ---------------------------------------------------------
def test_the_page_and_its_assets_are_served(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "Goldenrod" in page.text
    for asset in ("/static/style.css", "/static/app.js"):
        assert client.get(asset).status_code == 200


def _uncommented(source: str) -> str:
    """Source with its comments removed.

    These tests are about what the interface can put on screen, so a comment
    stating a rule must not read as a breach of it — app.js says "no confidence
    percentages, ever" at the top precisely because it must never render one.
    """
    import re

    source = re.sub(r"<!--.*?-->", " ", source, flags=re.S)
    source = re.sub(r"/\*.*?\*/", " ", source, flags=re.S)
    return re.sub(r"^\s*//.*$", " ", source, flags=re.M)


def test_the_page_never_shows_a_confidence_percentage():
    """CLAUDE.md rule 2. An LLM-produced number dressed as statistics ends the
    product in front of a professional, so it must not be possible to render one."""
    import re

    for name in ("index.html", "app.js", "style.css"):
        text = _uncommented((ROOT / "web" / name).read_text(encoding="utf-8"))
        assert not re.search(r"confidence", text, re.I), f"{name} mentions confidence"

    # Percentages are checked only where text is authored. A stylesheet uses %
    # for layout, which is not a claim about anything.
    for name in ("index.html", "app.js"):
        text = _uncommented((ROOT / "web" / name).read_text(encoding="utf-8"))
        assert not re.search(r"\b\d{1,3}\s?%", text), f"{name} renders a percentage"


def test_the_interface_uses_production_office_vocabulary():
    """CLAUDE.md rule 8: the brief is about enterprise friction. Say production
    office and departments; never filmmakers or creative partner."""
    import re

    text = "\n".join(
        (ROOT / "web" / name).read_text(encoding="utf-8")
        for name in ("index.html", "app.js")
    )
    for banned in ("filmmaker", "creative partner", "storyteller"):
        assert not re.search(banned, text, re.I), f"the interface says {banned!r}"


def test_the_interface_is_self_contained():
    """No CDN, no webfont, no build step. A demo that needs the network on the
    day is a demo that fails on the day."""
    import re

    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    external = re.findall(r'(?:src|href)="(https?://[^"]+)"', html)
    assert external == [], f"the page loads {external} from the network"
