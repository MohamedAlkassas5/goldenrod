# Fixtures

**Build this on day 1, before writing any service code.** Everything downstream depends on
it, and it is the only way anyone can work in parallel without waiting for the Extractor.

## What goes here

| File | What it is |
|---|---|
| `script-v1.pdf` / `.fountain` | A public-domain screenplay. Original revision. |
| `script-v2.fountain` | The same script with a change that breaks a **second-order reference** — something later in the script that depends on the changed fact. |
| `call-sheet.json` | The scenes scheduled to shoot "tomorrow". Include at least one scene that the v2 change breaks, and two that it doesn't. |
| `decisions.seed.json` | Prior decisions with real, specific reasons. At least one must relate to the planted break — that is what lets a finding cite a reason. |
| `commitments.seed.json` | Commitment state per entity. Vary it: something `shot`, something `built`, something `planned`. The ranking must have something to sort. |
| `access.seed.json` | Who on the crew is entitled to what. The roster only — the policy is code, in `services/common/access.py`. Access fails closed, so without this nobody can read anything. |
| `answer-key.json` | **Hand-written.** The findings a correct run must produce. |

## Why the answer key matters

You cannot evaluate what you have not defined. Write the expected findings by hand, before
the Gate exists, while you still have an unbiased view of what "correct" means. Once you
have seen the model's output it is very hard to write an honest key.

Format it as an array of `finding` objects (see `contracts/finding.schema.json`), with the
`evidence` filled in by hand from the script.

## Scoring

```
precision = correct findings / total findings produced
recall    = correct findings / findings in the answer key
```

**Target: precision ≥ 80%. Recall is secondary.** Three false positives kill adoption; one
missed catch does not. When tuning, bias toward silence.

## Choosing a script

Use something public domain so a judge can verify the catch against text they can read.
Whatever you pick, the planted break must be:

- **verifiable in seconds** by someone who has not read the whole script
- **second-order** — not just a renamed location, but something forty pages away that
  depends on it
- **connected to a seeded decision**, so the finding can cite a reason as well as a line
