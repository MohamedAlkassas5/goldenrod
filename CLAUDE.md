# CLAUDE.md

Read this before writing any code. `SPEC.md` is the full build document; this file is the
short version plus the rules that are easy to break by accident.

## What this project is

**Goldenrod** — a pre-commitment check for film/TV production. When tomorrow's call sheet is
issued, it checks every scene scheduled to shoot against the current script revision, the
film's established facts, and the production's logged decisions, and returns what this
week's changes already broke — **ranked by what has already been paid for**.

Tagline: *Catch it the night before, not in the cut.*

## Hard rules — do not break these

1. **No finding without evidence.** Every finding must carry at least one evidence object
   (scene + line, or a decision id). Enforce this in code, not in a prompt. Drop findings
   that fail the check.
2. **No confidence percentages.** Ever. Show raw counts or nothing.
3. **Findings are ranked by commitment state**, not by count or by model-assigned severity
   alone. `shot` > `built`/`cast` > `permitted`/`scouted` > `sourced` > `planned` > `none`.
4. **All ClickHouse access goes through the MCP server at runtime.** The rule, verbatim:
   code "must demonstrate the use of Google Cloud and the Partner services at runtime in
   your code — imported and actually called, not just named in the README." This is
   mechanically checkable and our track is judged by two ClickHouse engineers. No direct
   driver calls as a shortcut, not even temporarily.
5. **The SQL must be real analytical work** — aggregation over decision history, joins
   against commitment state, ranking. A `WHERE id = ?` lookup shown to these judges is worse
   than showing no SQL at all.
6. **Precision over recall.** Three false positives kill adoption; one missed catch does
   not. When tuning, bias toward silence.
7. **Never write secrets to the repo.** Use `.env`, which is gitignored. `.env.example`
   lists the keys with empty values.
8. **Enterprise vocabulary.** The hackathon brief is explicitly about "enterprise friction"
   and "enterprise chaos". In all copy, say *production office*, *departments*, *friction*,
   *governance*. Never *filmmakers* or *creative partner*.
9. **Role-scoped access is in scope.** One of the three builder roles the brief names is
   governance and IAM. Props sees only prop findings; the full script is access-controlled;
   every ledger write is attributed. Small, and it scores twice.

## Demo video — read before storyboarding

The rule is *"a demo video showing your project/agent functioning as built — **not a
cinematic trailer**."* Because the hackathon is called Agentic Cinema, many teams will make
a beautiful trailer and lose points. Ours is a **screen recording of working software** with
a voiceover. No montage, no title cards, no staged split-screen.

**Only the first 3 minutes are evaluated.** Never put the architecture frame at 2:55.

## Do NOT build these

Each is either already sold by a better-funded competitor or unbuildable honestly here.
Building any of them is how this project fails.

- Script breakdown / element tagging → Filmustage, Directure, SyncOnSet
- Stripboard, scheduler, budgeting → Movie Magic Scheduling
- Generic plot-hole or coverage report → StoryBirdie gives it away free
- Annotation transfer across drafts → Scriptation, ProductionPro
- "Director DNA" or any learned personal-style model
- An adversarial "I want to push back" persona
- Visual continuity from footage
- More than two agents

If a UI screen starts to look like a breakdown or a stripboard, stop and delete it.

## Architecture

Two agents only.

- **Extractor** — script → graph. Batch. Structured output against a fixed schema.
- **Gate** — the orchestrator. Runs on trigger, calls tools, does not free-associate.

Pipeline: parse day → diff drafts (fact-level, not text) → traverse dependencies → query
ledger → check commitment state → rank + cite.

## Contracts

`contracts/*.schema.json` are the boundaries between people's work. **Do not change a
contract without telling the team** — everyone codes against these, and fixtures depend on
them. If a change is needed, change the schema and the fixtures in the same commit.

## Repo layout

```
contracts/   JSON schemas — the interfaces between services
data/        fixtures: sample script, revision, and the hand-written answer key
db/          ClickHouse schema
services/    extractor · gate · api
web/         UI — the call sheet with findings attached
docs/        demo script
```

## Definition of done

See `SPEC.md` §8. The short version: the planted second-order break is caught with correct
citations on the first run, zero findings lack evidence, ranking visibly changes when a
commitment state changes, marking a finding intentional silences it and changes the re-run,
and precision on the answer key is ≥80%.

## Claims discipline

If you write user-facing copy, do not claim this prevents reshoots, and do not cite any
statistic about the cost of continuity errors — no reliable one exists. See `SPEC.md` §9.
