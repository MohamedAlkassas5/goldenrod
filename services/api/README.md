# api

**HTTP entry point for the interface.** Thin by design: it holds the ClickHouse MCP
connection, reads the call sheet, and hands the Gate's own output through. Detection,
ranking and SQL live in [`services/gate`](../gate/README.md), where they are testable
without a web server.

```bash
pip install -e ".[api]"
python -m services.api                    # http://localhost:8080
python -m services.api --reload           # develop against it
python -m services.api --production demo  # check a different production
```

## Endpoints

| Method | Path | What it does |
|---|---|---|
| `GET` | `/` | the call sheet page (`web/`) |
| `GET` | `/api/access/identities` | the roster, for the identity chooser — off behind IAP |
| `GET` | `/api/access/me` | who the platform says you are, and what that role may do |
| `GET` | `/api/access/log` | the audit trail. Needs `read_ledger` |
| `GET` | `/api/call-sheet` | the day, validated against its contract |
| `GET` | `/api/run` | the whole check, in one response |
| `GET` | `/api/run/stream` | the check, streamed — one SSE event per pipeline step, then the result |
| `POST` | `/api/findings/{id}/intentional` | accept a deviation, then re-run the identical check |
| `POST` | `/api/run/persist` | write the run's findings to the `findings` table |
| `GET` | `/api/graph` | scenes, facts, knowledge state, the ledger, commitment state |
| `GET` | `/api/health` | which MCP server and ClickHouse this instance is talking to |

Everything except `/` and `/api/health` needs an identity, and returns what that role is
entitled to and nothing else.

## The check fires on its own

`/api/run/stream` runs the check as the page loads. Nobody presses anything — the call
sheet being issued *is* the trigger (`SPEC.md` §3), and a check somebody has to remember
to run is a check that does not get run.

The steps are real. `run_gate` takes an `on_step` callback and the Gate is synchronous, so
it runs on a worker thread and pushes each completed stage into a queue that the SSE
generator drains. A step appears when that stage actually finished, and the `ms` beside it
is what it actually took. Each step also carries the SQL it sent and the first rows that
came back, because `SPEC.md` §7 asks for the query **and its result** on screen — a query
shown without its rows is a screenshot of intent rather than of work.

## One long-lived MCP connection

Spawning the MCP server per request would put a second of subprocess startup in front of
every page, so the app holds one client for its lifetime and reconnects if the server dies.
`ClickHouseMCP` serialises its own stdio pipe, so requests served from the thread pool
cannot read each other's replies.

## Configuration

All optional, all environment (see `.env.example`). Read on access rather than at import,
because the package builds its app the moment anything imports it — before `__main__` has
applied its flags.

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `8080` | port to bind |
| `PRODUCTION_ID` | — | override the call sheet's own `production_id` |
| `GOLDENROD_CALL_SHEET` | the fixture | path to the call sheet JSON |
| `GOLDENROD_PAGES` | the fixture | the revision's Fountain file, for citation quotes |
| `GOLDENROD_IDENTITY_CHOOSER` | `1` | accept a locally-chosen identity. Set to `0` behind IAP |

## `browse.py`

Kept apart from `services/gate/reader.py` on purpose. That module is the reads the *check*
makes, in pipeline order. This one is the reads a *person* makes when they open the graph
to see what Goldenrod knows. Both go through the MCP server; neither opens a connection.

## Role-scoped access

**This application authenticates nobody, and never should.** There is no password field,
no session store and no token anywhere in the codebase. It reads the identity its platform
has already proved — Cloud IAP writes `X-Goog-Authenticated-User-Email` on every request
that reaches the service — and looks that subject up in `access_grants` to get a role. The
header this reads is the header IAP writes, so the deploy needs no code change.

Locally there is no IAP, so `X-Goldenrod-Subject` and a same-origin cookie are accepted as
well, and the interface's identity chooser stands in for the platform's account picker.
Both are switched off with `GOLDENROD_IDENTITY_CHOOSER=0`.

The policy is code ([`services/common/access.py`](../common/access.py)); the roster is data
(`access_grants`, seeded from `data/*/access.seed.json`). A permission change is a
reviewable diff; adding a person is not a deploy.

`auth.py` resolves and refuses. `app.py` decides which routes need which permission. Neither
decides what a role may *see* — that is `services/common/access.py`, whose functions are
pure so a scoping bug is catchable without a web server. Enforcement is entirely
server-side: the chooser changes a cookie, not a filter.

Fail closed, everywhere. No identity is a 401; an unknown subject, an empty roster, a role
the policy no longer declares are all 403 — and every refusal is written to `access_log`
before it is returned, along with every successful read. The question a production office
asks is *who opened the pages last night*, and that has no answer unless the granted reads
are recorded too.
