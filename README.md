# Goldenrod

**Catch it the night before, not in the cut.**

Goldenrod checks every scene on tomorrow's call sheet against the current script revision,
the film's established facts, and the production's logged creative decisions — and tells you
what this week's changes already broke, ranked by what has already been paid for.

Built for **Agentic Cinema: The Blockbuster Hackathon** — ClickHouse track.

---

## The problem

Productions change constantly. Revisions arrive as coloured pages and each one silently
invalidates things established elsewhere in the script — and things the production has
already spent money on.

Move a character's home from Cairo to Alexandria and every breakdown tool re-tags the
location. What nobody catches is the line forty pages later referencing a Cairo landmark,
the travel time that no longer works, the picture vehicle already sourced with the wrong
plates, or the prep decision whose stated reason was the original location.

A change being *made* is not expensive. A change going unnoticed until money is *committed*
is.

## How it works

Goldenrod runs when the call sheet for the next day is issued. Nobody has to remember to use
it — the call sheet is made whether or not this exists.

```
call sheet issued
  → parse the day's scenes
  → diff current revision against the previous one, at the level of facts
  → traverse dependencies from each changed fact
  → query the decision ledger (ClickHouse, via MCP, at runtime)
  → check commitment state: shot / built / cast / scouted / permitted / planned
  → rank by cost, cite every claim
```

Every finding carries evidence — a scene, a line number, or a logged decision with the
reason given at the time. Findings without evidence are dropped.

What makes the second-order catch possible: the change lands in two scenes, and the damage
turns up in three others that nobody edited. A text diff points at the scenes that changed
and says nothing at all about the ones that broke.

## Stack

- **Gemini** — reasoning for the Extractor and Gate agents
- **Google Cloud / Gemini Enterprise Agent Platform** — agent runtime and orchestration
- **ClickHouse** — the graph, the decision ledger and commitment state, accessed through the
  ClickHouse MCP server at runtime. Nothing in this repo opens a ClickHouse connection of
  its own

## Running locally

> **This section is graded.** The rules require the repo to contain "all instructions needed
> to run". The steps below are tested. Sections marked TODO are not built yet.

### 1. Prerequisites

Python 3.11+, and a ClickHouse you can reach. Any instance works — ClickHouse Cloud, a
local binary, or Docker:

```bash
docker run -d --name goldenrod-ch -p 8123:8123 -p 9000:9000 clickhouse/clickhouse-server:latest
```

### 2. Install

```bash
pip install -e ".[dev]"
```

Configuration is environment variables throughout. Copy `.env.example` to `.env` and fill
in what you have — it is read at import and only ever *fills gaps*, so a real environment
variable always wins over the file, and a deployment's own configuration can never be
shadowed by a stray `.env`.

### 3. Install the ClickHouse MCP server

All database access goes through it — the application never opens a ClickHouse
connection of its own.

```bash
uv tool install mcp-clickhouse
```

`.mcp.json` launches it and already sets `CLICKHOUSE_ALLOW_WRITE_ACCESS=true` (needed for
INSERT) and `CLICKHOUSE_ALLOW_DROP=false` (destructive statements stay blocked). Point it at
your instance by exporting `CLICKHOUSE_HOST`, `CLICKHOUSE_PORT`, `CLICKHOUSE_USER`,
`CLICKHOUSE_PASSWORD`, `CLICKHOUSE_DATABASE`; the defaults target `localhost:8123`.

If `mcp-clickhouse` is not on your `PATH`, set the absolute path in `.mcp.json`.

### 4. Apply the schema

```bash
python -m services.loader.schema
```

Verify with `python -m services.loader.schema --check`.

### 5. Seed the ledger, commitment state and the access roster

```bash
python -m services.loader.seed
```

Loads `data/seed/` — prior decisions with real reasons, commitment state varied enough for
ranking to mean something, and who on the crew is entitled to what. Safe to run repeatedly:
`commitments` and `access_grants` are replaced in place, and `decisions` is append-only so
rows are matched by a deterministic id and skipped if already present. Inspect with
`python -m services.loader.seed --check`.

**Nobody can read anything until this runs.** Access fails closed: with no roster, every
request is refused rather than served.

### 6. Extract a graph from a screenplay

The Extractor turns a locked, scene-numbered Fountain script into a graph. Sluglines, scene
numbers, cues and line numbers are parsed deterministically; the facts, the knowledge state
and the dependencies come from Gemini, one structured-output pass per scene.

```bash
python -m services.extractor data/fixtures/script-v2.fountain --production fayoum --revision goldenrod-2026-08-29 -o data/fixtures/graph-v2.json
```

This needs Gemini credentials (`GOOGLE_CLOUD_PROJECT` for Vertex AI, or `GEMINI_API_KEY`)
and the optional SDK:

```bash
pip install -e ".[gemini]"
```

To check that a new screenplay parses before spending a model call, run the same command
with `--structure-only`: it emits scenes, entities and line numbers, and no facts.

Every citation the model returns is checked against the file before it is written. A fact
whose quote is not on the line it cites is dropped, not repaired into existence; what was
dropped and why is printed at the end of the run. See
[`services/extractor/README.md`](services/extractor/README.md).

### 7. Load a graph

```bash
python -m services.loader path/to/graph.json --dry-run
```

Drop `--dry-run` to write. Ingest validates against `contracts/graph.schema.json`, resolves
fact identity, writes through MCP, then verifies the database's computed `fact_key` matches
the loader's. It is idempotent for a given `revision_id`.

### 8. Run the check

The Gate is the trigger point: a call sheet in, findings out, ranked by what has already
been paid for and with evidence on every line.

```bash
python -m services.gate data/fixtures/call-sheet.json --pages data/fixtures/script-v2.fountain
```

It needs both revisions' graphs loaded for that production (steps 6 and 7, once each for
`script-v1.fountain` and `script-v2.fountain`) and the ledger seeded for it
(`python -m services.loader.seed --dir data/fixtures`). If the graph is missing the run
fails loudly rather than reporting an all-clear it has not earned.

`--pages` is optional and worth giving: the graph records the line a fact was established on
but not the words on it, so the pages are what turn a line number into a quotable citation.
Nothing is ever quoted from the model's paraphrase.

Useful flags:

```bash
python -m services.gate call-sheet.json --json -o run.json     # the run, for the interface
python -m services.gate call-sheet.json --write                # persist the findings
# accept a deviation, then re-run the identical check
python -m services.gate call-sheet.json --mark <finding_id> \
    --reason "Network note: the reveal holds to the grove" \
    --by script.coordinator@fayoum
```

Marking a finding intentional writes one attributed row to the ledger and silences that
finding on every later run. See [`services/gate/README.md`](services/gate/README.md) for the
pipeline, the three detection rules and the reasoning behind both.

### 9. Open the interface

The call sheet, with findings attached. This is the product surface — the pipeline runs as
the page loads, and nobody presses anything.

```bash
pip install -e ".[api]"
```

```bash
python -m services.api
```

Then open <http://localhost:8080>. Point it at a different production or a different day
without editing the fixtures:

```bash
python -m services.api --production demo --call-sheet path/to/call-sheet.json
```

See [`web/README.md`](web/README.md) for the design brief and
[`services/api/README.md`](services/api/README.md) for the endpoints.

### 10. Sign in as somebody else

An unreleased script is the most access-controlled document on a production, so every
route is scoped and every read is recorded.

The application authenticates nobody: it reads the identity the platform proved. Behind
Google Cloud IAP that arrives as `X-Goog-Authenticated-User-Email` on every request, which
is the header this reads — the deploy needs no code change. Locally there is no IAP, so the
switcher in the masthead stands in for the account picker. Pick the property master and
watch the same check come back scoped to props, with every quoted line redacted, the
returned rows withheld, and the sign-off button replaced by *Office sign-off required*.

It is enforced on the server, not on the page:

```bash
curl -s localhost:8080/api/run                                     # 401, no identity
curl -s localhost:8080/api/run -H 'X-Goldenrod-Subject: props@fayoum' | jq '.findings[0].evidence'
```

Roles and their permissions are declared in
[`services/common/access.py`](services/common/access.py); who holds which role is data, in
`access_grants`, seeded from `data/*/access.seed.json`. Set
`GOLDENROD_IDENTITY_CHOOSER=0` to turn the local chooser off, which is what a deployment
behind IAP does.

### 11. Tests

```bash
python -m pytest tests -q
```

Integration tests skip automatically when no ClickHouse is reachable. `tests/test_gate.py`
scores a full run against the hand-written answer key — that number is the acceptance
criterion in `SPEC.md` §8, and it is computed rather than asserted by hand.

### 12. Deploy

Cloud Run, against ClickHouse Cloud, built from source by Cloud Build — no local Docker
daemon needed. Gemini goes through Vertex AI on the service's own identity, so no API key
exists anywhere in the deployment.

```bash
gcloud run deploy goldenrod --source .
```

The full command with its flags, the IAM the service needs, how to load a production into
ClickHouse Cloud, and what changes when you put Cloud IAP in front of it: see
[`deploy/README.md`](deploy/README.md).

The container launches the same `mcp-clickhouse` server from the same
[`.mcp.json`](.mcp.json) that runs on a laptop. There is one code path to ClickHouse and it
is the MCP server, in both places.

### Not built yet

- TODO: a hosted URL. The deploy is written and repeatable; the service is not up yet

`data/fixtures/graph-v1.json` and `graph-v2.json` are not committed: they are the Extractor's
output, and generating them honestly needs Gemini credentials. Run step 6 against both
fixture screenplays to produce them.

## Repo layout

| Path | What's in it |
|---|---|
| `SPEC.md` | Full build document — data model, pipeline, plan, acceptance criteria |
| `CLAUDE.md` | Working rules and the do-not-build list |
| `contracts/` | JSON schemas — the interfaces between services |
| `data/seed/` | Demo seed: decisions, commitment state and the access roster (pre-fixture) |
| `data/fixtures/` | Sample script, revision, and the hand-written answer key |
| `db/clickhouse/` | Table definitions |
| `services/` | `common` (MCP client, canonical SQL, access policy) · `extractor` (script → graph) · `loader` (graph ingest + ledger seed) · `gate` (the check) · `api` (HTTP + identity) |
| `web/` | Interface — the call sheet with findings attached |
| `docs/` | Demo script |

## What this is not

Not a script breakdown tool, a scheduler, a budgeting system, or a screenwriting assistant.
Goldenrod is the check that runs over the tools a production already uses.

## Licence

MIT — see [LICENSE](LICENSE).
