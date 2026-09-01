# Demo seed — ledger and commitment state

**This is not the screenplay fixture set.** It is the pre-fixture seed that lets the ledger
query, the commitment ranking and the Gate be built and demonstrated before a screenplay
exists. It is deliberately kept out of `data/fixtures/`, which is reserved for the real
public-domain script, its revision, the call sheet and the hand-written answer key.

| File | What it is |
|---|---|
| `decisions.seed.json` | Prior decisions with real, specific reasons |
| `commitments.seed.json` | Commitment state per entity, varied so ranking has something to sort |
| `access.seed.json` | Who holds which role. The roster is data; the policy is code, in `services/common/access.py` |

Both validate against [`contracts/seed.schema.json`](../../contracts/seed.schema.json).

## Load it

```bash
python -m services.loader.seed
```

Safe to run repeatedly — see *Idempotency* below.

## The placeholder world

Scene numbers and entity ids match the planted-break shape already used across the test
suite, so the seed and the graph line up:

- **scene 18** — where the letter fact was established in v1
- **scene 24** — shoots tomorrow, refers to the letter, and is already `shot`
- **scene 31** — where the v2 goldenrod revision moved the reveal to
- **scene 12** — an unaffected scene, so the ranking has something to *not* flag

Entities: `entity:rana` (character), `entity:letter` (prop), `entity:flat` (location),
`entity:street` (location).

When the real screenplay lands, these ids get replaced with the ones the Extractor emits and
this directory is superseded by `data/fixtures/*.seed.json`. Nothing else has to change —
the loader reads whatever the contract allows.

## Why one decision matters more than the rest

`withhold-letter-reveal` is the decision the planted second-order break hangs on. It is
what lets a finding cite a *reason* — "Withhold the letter reveal until the act-two turn"
— rather than only a scene and a line. Per `data/fixtures/README.md`, at least one seeded
decision must connect to the planted break; this is it.

## Idempotency

`commitments` is a `ReplacingMergeTree` keyed on `(production_id, entity_id, scene_id)`, so
re-seeding replaces rows naturally.

`decisions` is a plain `MergeTree` — deliberately, because a ledger is append-only and
rewriting history would defeat the point. That means a naive re-seed would duplicate every
row. So each seed entry carries a stable `seed_key`, the loader derives its `decision_id` as
a UUIDv5 of `production_id|seed_key`, and it skips any decision whose id is already present.

Re-running is therefore safe without any `UPDATE` or `DELETE` — which matters, because the
MCP server runs with `CLICKHOUSE_ALLOW_DROP=false` and destructive statements are refused.

## Editing

Change a value in place and re-run: for `commitments` the row is replaced; for `decisions`
it is **not**, because the id already exists and the row is skipped. To change a seeded
decision, give it a new `seed_key` and set the old one's `status` to `superseded` — which is
how a real ledger records a change of mind, and why `status` exists.
