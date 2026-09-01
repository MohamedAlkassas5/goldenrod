# gate

**Call sheet in, ranked and cited findings out.** The second of the two agents
(`CLAUDE.md`: *two agents only*). It is the orchestrator: it runs on a trigger, calls tools,
and does not free-associate. There is no model call anywhere in this package.

```bash
python -m services.gate data/fixtures/call-sheet.json \
    --pages data/fixtures/script-v2.fountain
```

## The pipeline

Six steps, each producing structured output the next one consumes. Every step records what
it did, how long it took and the SQL it sent, on `GateRun.steps` — that is what the
interface renders as the check runs, and it is why this is inspectable rather than a chat
wrapper.

| # | Step | What it does | Where |
|---|---|---|---|
| 1 | `parse_day` | the scene numbers scheduled for the shoot date | `run.py` |
| 2 | `draft_diff` | fact-level change list, current vs prior revision | `draft_diff` in `schema.sql` |
| 3 | `traverse` | scenes that depend on a changed fact | `dependent_scenes` in `schema.sql` |
| 4 | `ledger` | decisions touching them, with the reason given at the time | `commitment_ranking` in `schema.sql` |
| 5 | `commitment` | what has already been paid for, per scene | `scene_commitments` in `schema.sql` |
| 6 | `rank` | order by commitment cost, evidence on every claim | `findings.py` |

All four queries are extracted from `db/clickhouse/schema.sql` at runtime by
`services/common/queries.py`, so the file a judge reads is the file that runs, and every one
of them goes through the ClickHouse MCP server.

## The trick: traversal reads *both* revisions

This is the part worth understanding, because it is the whole catch and it is not obvious.

The Extractor visits scenes in script order, so a scene can only depend on a fact
established **before** it. When the goldenrod revision moves the letter reveal from scene 18
to scene 31, the edges `22 → letter` and `24 → letter` become impossible to express — those
scenes now come first. The dependency has not left the pages. It has left what the graph can
say.

**That silence is the break.** So the Gate reads those edges out of the prior revision, where
they could be expressed, and carries them onto the current pages (`evidence.carry_forward`):

- the fact side is replaced with the fact as it now stands — the scene it moved to and the
  line it is on there;
- the scene side keeps its quote, which is still verbatim in the pages, and its line number
  is re-found in the current file, because a revision re-flows every line below the change;
- an edge from a scene the writer actually edited is **not** carried forward at all. Scene
  text hashes decide that. They may have fixed it, and flagging a scene somebody has just
  rewritten is the false positive that ends adoption.

## The three rules

Deliberately three, and deliberately narrow (`CLAUDE.md` rule 6 — bias toward silence).

| Rule | Fires when | Finding kind |
|---|---|---|
| `out_of_order_reference` | a fact moved later (or arrived new) and a scene that depends on it now sits before it | `knowledge_state`, or `reference_break` |
| `restated_fact` | a fact's statement changed and a scene after it assumes the old wording | `temporal`, or `fact_contradiction` |
| `dangling_reference` | a fact was removed and a scene still in the pages referenced it | `reference_break` |

Nothing fires on:

- a scene depending on an **unchanged** fact, however wrong the ordering looks. That is a
  pre-existing property of the script, not something this week's revision broke;
- the scene the revision **changed**. The writer meant that; it shoots against the current
  pages and is correct as scheduled;
- any scene whose number cannot be placed in script order. A claim that rests on ordering is
  not made when the ordering is unknown.

Script order comes from the locked scene number (`scene_order.py`): `A24 → 24 → 24A → 25`.
Nothing stores an ordinal because on a locked script the number *is* the order — see the
identity note at the top of `db/clickhouse/schema.sql`.

## Evidence, and the drop rule

`CLAUDE.md` rule 1 is enforced here, in code:

- a finding with an empty evidence array is **dropped**, and so is one the finding contract
  rejects for any other reason;
- every `scene_line` quote is copied out of a file — either the dependency's own quote,
  which the Extractor verified at extraction time, or a line read back out of the pages;
- a decision with no `reason` is not cited, because it cannot support a claim about why;
- what was dropped, and why, is on `GateRun.dropped`. Nothing is swallowed silently.

`--pages` is optional and worth giving. The graph stores the line a fact was established on
but not the words on it — `facts.statement` is the model's paraphrase and is not admissible
as a quote. Without the pages, a carried citation keeps the prior revision's line number and
is tagged with that revision id, which is still a real line in a real file, honestly
labelled. Nothing is ever filled in from the paraphrase.

## Ranking and severity

Findings are ordered by commitment state, never by count and never by a severity a model
chose. The rank is `commitmentRank`, computed by ClickHouse; this package never maps a state
to a number. Ties break on whether the scene shoots tomorrow, then on script order, so two
runs over the same data always render identically.

Severity is derived from that rank: `0–1` (shot, built, cast) is high, `2–4` (permitted,
scouted, sourced, planned) is medium, `5` (nothing committed) is low. An element already in
the can cannot be changed without a company day; one on the one-liner changes with an email.

Each finding also carries the crew **departments** that own the elements the break is about,
from the element types in the `entities` table. That is what makes role-scoped access a
property of the finding rather than something the API re-derives per request. The mapping
and the roles live in [`services/common/access.py`](../common/access.py); the Gate does not
know or care who is looking.

## Marking a finding intentional

```bash
python -m services.gate call-sheet.json --mark <finding_id> \
    --reason "Network note: the reveal holds to the grove" \
    --by script.coordinator@fayoum
```

The write goes to the **ledger only**. `decisions` is append-only on purpose and the MCP
server refuses destructive statements, so there is no `UPDATE` in this project and there is
not meant to be. Suppression is computed at run time instead: the dismissal's `decision_id`
is a deterministic function of the finding id, and the next run asks the ledger whether that
id is present. Nothing has to be kept in sync.

Both ids are stable by construction. `finding_id` is a UUIDv5 of
`(production, scene, rule, fact match key)` and deliberately not of the shoot date — the same
break on a later call sheet is the same break, and a deviation accepted yesterday stays
silent tomorrow.

Because a dismissal is a real decision with a real reason, a finding on the *same fact* in
another scene picks it up as evidence and its suggested action changes: once the office has
signed off scene 24 on the basis that the letter reads in 31, the right question about scene
22 is a pickup, not a rewrite.

Every write is attributed and reasoned. An unexplained dismissal is refused, and through
the API the signature is not typed at all — it is the identity the platform proved, so
nobody can sign as anybody. This is an unreleased script, and that is what the governance
half of the brief is about.

## Tests

`tests/test_gate.py`. The unit half runs anywhere: the rules, the ordering and the evidence
enforcement are pure functions over rows, so precision can be tuned without a database or a
model. The integration half drives the whole pipeline over the real fixture screenplays,
through the MCP server, and **scores the result against `data/fixtures/answer-key.json`** —
that score is the acceptance criterion in `SPEC.md` §8.
