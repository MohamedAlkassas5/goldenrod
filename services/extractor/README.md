# extractor

**Script → graph.** One of the two agents (`CLAUDE.md`: *two agents only*). Batch, not
interactive: it runs on ingest and on every new revision, one structured-output pass per
scene against a fixed schema.

```bash
python -m services.extractor data/fixtures/script-v2.fountain \
    --production fayoum --revision goldenrod-2026-08-29 \
    -o data/fixtures/graph-v2.json

python -m services.loader data/fixtures/graph-v2.json
```

Add `--structure-only` to parse a script into scenes and entities without spending a model
call — worth doing once on any new screenplay before paying to extract it.

## The split

| Half | How | Where |
|---|---|---|
| Sluglines, scene numbers, cues, dialogue, line numbers | parsed, deterministically | `fountain.py` |
| Entity ids | derived by rule from names | `entities.py` |
| Facts, knowledge state, dependencies | Gemini, structured output, one pass per scene | `prompt.py` · `gemini.py` |
| Whether any of it survives | checked against the file | `extract.py` |

Structure is *parsed*, never inferred. A model asked to reproduce a scene number only adds a
way to get it wrong, and scene number is the coordinate the entire fact-level diff rests on
(`db/clickhouse/schema.sql`). The model is asked for one thing: the facts.

## Cite or drop

`CLAUDE.md` rule 1 says a finding needs evidence and that the rule is enforced in code, not
in a prompt. A finding can only cite what the graph holds, so the rule bites here first:

- a fact whose quote is not on the line it cites is **dropped**
- a dependency whose evidence line is not in the scene is **dropped**
- a dependency on a fact from its own scene is **dropped** — second-order or nothing
- a `knowledge_state` entry pointing at a fact that does not exist is **dropped**
- a fact with no resolvable entity is **dropped** — nothing can depend on it, and nothing
  has been paid for it

There is exactly one repair. If a quote is not on the cited line but appears on **exactly
one** other line of the same scene, the line number is corrected to that line: the evidence
still comes from the file, only the model's arithmetic is fixed. Ambiguous or absent, and
the item goes. The stored quote is always read back out of the script, never taken from the
model.

Everything dropped is reported by scene with a reason (`ExtractionReport.summary()`). That
is the tuning loop for precision — you cannot bias toward silence without seeing what
silence is costing.

## Entity ids are the join to the money

`commitments` and `decisions` are keyed on `entity_id`. If the Extractor invents a different
id for the same picture vehicle than the one the production office logged against, the
commitment lookup returns nothing and every finding ranks `none`. So ids are derived by a
rule, not by a model:

```
entity_id = "entity:" + slug(name)
slug: casefold · drop a leading article · non-alphanumerics to "-" · collapse · trim
```

`the Fayoum land` → `entity:fayoum-land`. `TIN BOX` → `entity:tin-box`. Sub-locations commit
to the practical, not the room: `FLAT, KITCHEN` and `FLAT, LIVING ROOM` are both
`entity:flat`, because that is one agreement and one access window. Surface-form drift is
absorbed by `aliases`.

This is **not** script breakdown (`CLAUDE.md` do-not-build list). It resolves names the
parser already found onto ids. It does not tag elements and it does not go hunting for props
in action lines.

## Locked pages only

Every slugline must carry its scene number — `INT. FLAT, KITCHEN - NIGHT #22#`. One without
is refused rather than given a made-up coordinate. Goldenrod runs on locked scripts, where
scene numbers never move: inserted scenes take letters (`24A`), cut scenes become
`24 OMITTED`.

## Credentials

`GOOGLE_CLOUD_PROJECT` + `GOOGLE_CLOUD_LOCATION` routes through Vertex AI; `GEMINI_API_KEY`
uses the Gemini API. `GEMINI_MODEL` picks the model. See `.env.example`. The SDK is an
optional dependency:

```bash
pip install -e ".[gemini]"
```

## Tests

`tests/test_extractor.py` drives the whole Extractor with a scripted backend standing in
for the model, over the real fixture screenplays. The model is the only part that needs a
network, and it is the only part not covered.
