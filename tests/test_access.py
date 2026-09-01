"""Role-scoped access: the policy on its own, and the policy actually enforced.

Two halves, deliberately separated.

The first needs no database and no web server, because the scoping rules are
pure functions (services/common/access.py) — and a bug here is a leaked page
rather than a wrong answer, so it has to be testable at that level.

The second is the half that matters: that the *server* enforces it. A filter
applied only in the browser is a filter that a curl command walks straight
through, so every one of these asks the API, with an identity, and checks what
came back over the wire.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from services.common.access import (
    REDACTED,
    ROLES,
    departments_for,
    get_role,
    redact,
    scope_findings,
    scope_graph,
    scope_run,
    scope_step,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "data" / "fixtures"

OFFICE = ROLES["script_coordinator"]
PROPS = ROLES["props"]
PRODUCER = ROLES["producer"]
LOCATIONS = ROLES["locations"]


def finding(scene: str, departments: list[str]) -> dict:
    return {
        "finding_id": f"f-{scene}",
        "scene": scene,
        "shoot_date": "2026-09-04",
        "severity": "high",
        "commitment_state": "shot",
        "kind": "knowledge_state",
        "claim": "Something broke.",
        "departments": departments,
        "evidence": [
            {
                "type": "scene_line",
                "scene": scene,
                "line": 98,
                "quote": "She sold it. Was it you who did the papers?",
            },
            {"type": "decision", "decision_id": "d1", "reason": "Network note."},
        ],
    }


# --- the policy ------------------------------------------------------------
def test_departments_come_from_the_element_types():
    assert departments_for(["prop", "character"]) == ["cast", "props"]
    assert departments_for(["vehicle"]) == ["transport"]


def test_an_unmapped_element_type_does_not_leak_into_a_department():
    """Fail closed: an unknown type must not land in whichever sorts first."""
    assert departments_for(["hovercraft"]) == ["script"]
    assert departments_for([]) == ["script"]


def test_an_unknown_role_name_denies_rather_than_defaulting():
    assert get_role("studio_head") is None
    assert get_role("") is None


def test_a_department_sees_its_own_findings_and_not_the_rest():
    findings = [finding("22", ["props"]), finding("24", ["cast"])]
    released, note = scope_findings(findings, PROPS)
    assert [f["scene"] for f in released] == ["22"]
    assert note["withheld"] == 1


def test_the_office_sees_everything_including_story_only_findings():
    findings = [finding("22", ["props"]), finding("33", ["script"])]
    released, note = scope_findings(findings, OFFICE)
    assert [f["scene"] for f in released] == ["22", "33"]
    assert note["withheld"] == 0


def test_a_role_with_no_stake_in_the_revision_sees_nothing_at_all():
    released, note = scope_findings([finding("22", ["props"])], LOCATIONS)
    assert released == []
    assert note["withheld"] == 1


def test_redaction_removes_the_page_and_keeps_the_citation():
    """CLAUDE.md rule 1 interacts with this: a finding must still cite.

    Dropping the evidence object instead of its quote would leave a finding
    citing nothing, which the drop rule then deletes — an access rule quietly
    turning into a missed catch.
    """
    scoped = redact(finding("22", ["props"]), PROPS)
    line = scoped["evidence"][0]
    assert line["quote"] == REDACTED
    assert (line["scene"], line["line"]) == ("22", 98)
    assert len(scoped["evidence"]) == 2
    assert scoped["redacted"] is True


def test_the_office_sees_the_page_unredacted():
    scoped = redact(finding("22", ["props"]), OFFICE)
    assert "sold it" in scoped["evidence"][0]["quote"]
    assert "redacted" not in scoped


def test_a_pipeline_step_keeps_its_sql_and_loses_its_rows():
    """The SQL names ids and fact keys; the rows it returned carry the pages."""
    step = {
        "name": "draft_diff", "rows": 3, "sql": "SELECT fact_key FROM facts",
        "sample": [{"statement": "Rana has read the letter"}],
    }
    scoped = scope_step(step, PROPS)
    assert scoped["sample"] == []
    assert scoped["sample_withheld"] == 3
    assert scoped["sql"] == step["sql"]
    assert scope_step(step, OFFICE) == step


def test_scoping_a_run_covers_every_surface_that_carries_the_script():
    run = {
        "findings": [finding("22", ["props"]), finding("24", ["cast"])],
        "dismissed": [finding("31", ["cast"])],
        "steps": [{"name": "draft_diff", "rows": 2, "sample": [{"statement": "x"}]}],
        "dropped": ["22/knowledge_state: quote not on line 98"],
    }
    scoped = scope_run(run, PROPS)
    assert [f["scene"] for f in scoped["findings"]] == ["22"]
    assert scoped["dismissed"] == []
    assert scoped["steps"][0]["sample"] == []
    assert scoped["dropped"] == []
    # withheld counts the silenced one too: two things exist that props may not see
    assert scoped["access"]["withheld"] == 2
    assert scoped["access"]["role"]["name"] == "props"


def test_scoping_a_run_leaves_the_office_view_untouched():
    run = {
        "findings": [finding("22", ["props"])],
        "dismissed": [],
        "steps": [{"name": "draft_diff", "rows": 1, "sample": [{"statement": "x"}]}],
        "dropped": ["a note"],
    }
    scoped = scope_run(run, OFFICE)
    assert scoped["steps"][0]["sample"] == [{"statement": "x"}]
    assert scoped["dropped"] == ["a note"]
    assert scoped["access"]["withheld"] == 0


def test_browse_withholds_the_pages_and_the_reasons_but_not_the_day():
    graph = {
        "scenes": [{"scene_number": "18"}],
        "entities": [{"entity_id": "entity:letter"}],
        "facts": [{"statement": "Rana has read the letter"}],
        "knowledge_state": [{"knows": 1}],
        "dependencies": [{"evidence_quote": "You read what she wrote."}],
        "decisions": [{"reason": "Network note."}],
        "commitments": [{"state": "shot"}],
    }
    scoped = scope_graph(graph, PROPS)
    assert scoped["scenes"] and scoped["entities"] and scoped["commitments"]
    assert scoped["facts"] == scoped["knowledge_state"] == scoped["dependencies"] == []
    assert scoped["decisions"] == []
    assert set(scoped["access"]["withheld_surfaces"]) == {
        "facts", "knowledge_state", "dependencies", "decisions",
    }
    assert scope_graph(graph, OFFICE)["facts"] == graph["facts"]


def test_oversight_and_authority_are_separate_grants():
    """A producer reads everything and signs nothing."""
    assert PRODUCER.read_script and PRODUCER.read_ledger
    assert not PRODUCER.write_ledger
    assert ROLES["script_coordinator"].write_ledger


# --- the identity the platform proved --------------------------------------
def test_the_iap_header_is_parsed_and_wins_over_the_local_one(monkeypatch):
    from services.api.auth import subject_from_headers

    monkeypatch.delenv("GOLDENROD_IDENTITY_CHOOSER", raising=False)
    assert (
        subject_from_headers(
            {"x-goog-authenticated-user-email": "accounts.google.com:Props@Fayoum"}
        )
        == "props@fayoum"
    )
    # A client cannot talk over IAP by adding a header of its own.
    assert (
        subject_from_headers(
            {
                "x-goog-authenticated-user-email": "accounts.google.com:props@fayoum",
                "x-goldenrod-subject": "first.ad@fayoum",
            }
        )
        == "props@fayoum"
    )


def test_turning_the_chooser_off_ignores_the_local_header(monkeypatch):
    """What a deploy behind IAP does. Identity then comes from the platform only."""
    from services.api.auth import subject_from_headers

    monkeypatch.setenv("GOLDENROD_IDENTITY_CHOOSER", "0")
    assert subject_from_headers({"x-goldenrod-subject": "first.ad@fayoum"}) == ""


# --- enforced by the server, not by the page -------------------------------
pytest.importorskip("fastapi", reason='the API needs: pip install -e ".[api]"')

from fastapi.testclient import TestClient  # noqa: E402

from services.api import create_app  # noqa: E402

PRODUCTION = "test_access"


@pytest.fixture(scope="module")
def loaded():
    from services.common.mcp_client import ClickHouseMCP, MCPError
    from tests.demo_graph import load_production

    try:
        client = ClickHouseMCP().connect()
        client.run_query("SELECT 1")
    except (MCPError, OSError, FileNotFoundError) as exc:
        pytest.skip(f"no ClickHouse reachable through MCP: {exc}")
    load_production(client, PRODUCTION)
    yield client
    client.close()


@pytest.fixture
def app_client(loaded, monkeypatch):
    monkeypatch.setenv("PRODUCTION_ID", PRODUCTION)
    monkeypatch.setenv("GOLDENROD_CALL_SHEET", str(FIXTURES / "call-sheet.json"))
    monkeypatch.setenv("GOLDENROD_PAGES", str(FIXTURES / "script-v2.fountain"))
    monkeypatch.setenv("GOLDENROD_IDENTITY_CHOOSER", "1")
    with TestClient(create_app()) as c:
        yield c


def as_(subject: str) -> dict[str, str]:
    return {"X-Goldenrod-Subject": subject}


@pytest.mark.mcp
def test_an_unidentified_request_gets_nothing(app_client):
    for path in ("/api/run", "/api/graph", "/api/call-sheet"):
        assert app_client.get(path).status_code == 401, path


@pytest.mark.mcp
def test_an_identity_with_no_grant_on_this_production_is_refused(app_client):
    response = app_client.get("/api/run", headers=as_("stranger@elsewhere"))
    assert response.status_code == 403
    assert "no grant" in response.json()["detail"]


@pytest.mark.mcp
def test_props_gets_its_own_findings_with_the_pages_removed(app_client):
    office = app_client.get("/api/run", headers=as_("script.coordinator@fayoum")).json()
    props = app_client.get("/api/run", headers=as_("props@fayoum")).json()

    assert office["findings"], "the fixture should produce findings to scope"
    assert all("props" in f["departments"] for f in props["findings"])
    for f in props["findings"]:
        quotes = [e["quote"] for e in f["evidence"] if e["type"] == "scene_line"]
        assert quotes and all(q == REDACTED for q in quotes)
        assert f["evidence"], "redaction must never leave a finding uncited"
    assert props["access"]["script_redacted"] is True
    assert all(step["sample"] == [] for step in props["steps"])


@pytest.mark.mcp
def test_the_location_manager_sees_nothing_this_revision_touched(app_client):
    scoped = app_client.get("/api/run", headers=as_("location.manager@fayoum")).json()
    assert scoped["findings"] == []
    assert scoped["access"]["withheld"] > 0


@pytest.mark.mcp
def test_a_department_head_cannot_accept_a_deviation(app_client):
    """Seeing the break you own and signing it off are different grants."""
    office = app_client.get("/api/run", headers=as_("script.coordinator@fayoum")).json()
    target = office["findings"][0]["finding_id"]
    for subject in ("props@fayoum", "line.producer@fayoum"):
        response = app_client.post(
            f"/api/findings/{target}/intentional",
            json={"reason": "trying it on"},
            headers=as_(subject),
        )
        assert response.status_code == 403, subject


@pytest.mark.mcp
def test_browse_is_scoped_server_side(app_client):
    graph = app_client.get("/api/graph", headers=as_("props@fayoum")).json()
    assert graph["scenes"], "the day is not a secret"
    assert graph["facts"] == [] and graph["decisions"] == []
    assert "facts" in graph["access"]["withheld_surfaces"]


@pytest.mark.mcp
def test_the_audit_trail_records_both_the_reads_and_the_refusals(app_client):
    """The question a production office asks is who opened the pages last night.

    That has no answer unless the successful reads are logged too, so this
    checks for both: a granted check by props, and the refusal that follows when
    props tries to read the trail itself.
    """
    app_client.get("/api/run", headers=as_("props@fayoum"))
    assert (
        app_client.get("/api/access/log", headers=as_("props@fayoum")).status_code == 403
    )

    trail = app_client.get(
        "/api/access/log", headers=as_("script.coordinator@fayoum")
    ).json()["trail"]
    by_props = [r for r in trail if r["subject"] == "props@fayoum"]
    assert any(r["action"] == "check" and int(r["granted"]) == 1 for r in by_props)
    assert any(r["action"] == "denied" and int(r["granted"]) == 0 for r in by_props)


@pytest.mark.mcp
def test_the_identity_chooser_can_be_switched_off_for_a_deployment(
    app_client, monkeypatch
):
    """Behind IAP the platform picks; offering a chooser would be theatre."""
    assert app_client.get("/api/access/identities").json()["identities"]
    monkeypatch.setenv("GOLDENROD_IDENTITY_CHOOSER", "0")
    assert app_client.get("/api/access/identities").status_code == 404
    # and the local header stops working, so nothing is reachable without IAP
    assert app_client.get("/api/run", headers=as_("props@fayoum")).status_code == 401
