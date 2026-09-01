# SPEC.md — Goldenrod

> **Working name.** `ReelMind` is unusable — reelmind.ai is a live AI video platform.
> **Goldenrod** is the WGA revision colour issued for production-driven changes (location
> changes, actor availability, network notes). Industry-native, instantly recognised by
> anyone who has worked in a production office, and it names exactly the thing we act on.
> Alternates if it's taken: `Lastlook`, `Tramline`, `Nightwatch`.

**Agentic Cinema: The Blockbuster Hackathon — ClickHouse track**

Deadline: **9 September 2026, 2:00 PM PT** — which is **midnight on the night of Wed 9 → Thu
10 September, Cairo time.** There is no working day on the 10th. Submit Tuesday the 8th.

The hackathon's own framing is **enterprise**: *"transform real-world enterprise chaos,"*
*"a deterministic, multi-step agent that solves enterprise friction."* Write and speak
accordingly — this is a **production office** workflow, not a creativity tool. Say
"production office", "departments", "friction", "governance". Do not say "filmmakers" or
"creative partner".

---

## 1. What we are building

A **pre-commitment check** for film and TV production.

When tomorrow's call sheet is issued — around 7pm the night before — Goldenrod takes every
scene scheduled to shoot, checks it against the current script revision, the film's
established facts, and the production's logged creative decisions, and returns what is at
risk. Findings are **ranked by what has already been paid for**, and every line cites a
scene, a line number, or a logged decision.

**The one-sentence pitch:** *Nothing shoots tomorrow that this week's revision already broke.*
**The closing line:** *Catch it the night before, not in the cut.*

### Why this moment

A change being *made* is not expensive. A change going unnoticed until money is *committed*
is. The cost curve is a step function: near zero before the shoot day, an entire day or a
pickup after it. So the check fires at the commitment boundary, not continuously.

### Who it is for

The **1st AD**, the **script coordinator**, and the **production coordinator**. Not the
director — the director has no bandwidth between takes, and the script supervisor already
does on-set continuity better than we ever will.

---

## 2. What we are explicitly NOT building

Every item here is either already sold by someone better funded, or unbuildable honestly in
the time we have. Building any of them is how this project fails.

| Do not build | Because |
|---|---|
| Script breakdown / element tagging | Filmustage, Directure, SyncOnSet. We'd ship a worse version |
| Stripboard, scheduler, budget | Movie Magic Scheduling. Not our problem |
| Generic plot-hole / coverage report | StoryBirdie gives it away free |
| Annotation transfer across drafts | Scriptation, ProductionPro |
| "Director DNA" / learned style model | Preference learning needs 30–300 clean pairs minimum; one film gives us a few hundred noisy, confounded ones |
| Any confidence percentage | An LLM-produced number dressed as statistics. One wrong "87%" in front of a professional ends the product |
| Adversarial "I want to push back" persona | Status attack on set; research shows challenging the *AI's own recommendation* works, challenging the human does not |
| Visual continuity from footage | We have no footage and no time |
| Five specialist agents | Costs days, adds latency, wins no points. Two agents maximum |

**Rule of thumb:** if a judge sees a breakdown screen or a stripboard, we are being compared
to Filmustage, and we lose that comparison. Never show one.

---

## 3. The pipeline

Deterministic, multi-step, inspectable. Every stage produces structured output that the next
stage consumes. This is what separates us from a chat wrapper — and the intermediate state
must be visible in the UI.

```
TRIGGER: call sheet issued (or manual "check tomorrow")
   │
   ├─ 1. PARSE DAY        → scene IDs scheduled for the shoot date
   │
   ├─ 2. DRAFT DIFF       → fact-level change list between current and prior revision
   │                        (NOT a text diff — a list of facts that changed)
   │
   ├─ 3. TRAVERSE         → for each changed fact, dependent scenes + entities
   │                        via the dependency graph
   │
   ├─ 4. LEDGER QUERY     → ClickHouse, via MCP server, at runtime:
   │                        prior decisions touching those scenes/entities,
   │                        with the reason given at the time
   │
   ├─ 5. COMMITMENT LOOKUP→ current state of each affected element:
   │                        shot / built / cast / scouted / permitted / planned / none
   │
   └─ 6. RANK + CITE      → order by commitment cost, attach evidence to every claim
   │
OUTPUT: findings list, ranked
   │
HUMAN: fix · accept · mark intentional  →  writes back to ledger as new evidence
```

### Agents (two, not five)

- **Extractor** — script → graph. Runs on ingest and on every new revision. Structured
  output against a fixed schema. Batch, not interactive.
- **Gate** — the orchestrator above. Runs on trigger. Calls tools; does not free-associate.

---

## 4. Data model

### 4.1 Film graph

Store in Cloud SQL / Postgres (or ClickHouse if we want one system — fine for a hackathon).

```sql
scene(
  scene_id, production_id, revision_id, scene_number, int_ext,
  location_id, day_night, page_eighths, synopsis, text_hash
)

entity(
  entity_id, production_id, type, name, aliases[]
)
-- type: character | location | prop | costume | vehicle | set | symbol

scene_entity(
  scene_id, entity_id, role, mention_lines[]
)

fact(
  fact_id, production_id, statement, kind,
  established_in_scene_id, source_line, revision_id
)
-- kind: world | relationship | possession | knowledge | physical | temporal

knowledge_state(
  character_entity_id, fact_id, scene_id, knows BOOL, acquired_via
)
-- the "who knows what, when" table. This is the one that catches the good stuff.

dependency(
  dependency_id, from_fact_id, to_scene_id, kind, evidence_line, confidence_note
)
-- kind: references | assumes | contradicts_if_changed
```

**`knowledge_state` and `dependency` are the product.** Everything else is bookkeeping.
Get these two right before touching anything else.

### 4.2 Decision ledger — ClickHouse

The only reason our findings can say *why*. No competitor stores this.

```sql
CREATE TABLE decisions (
  decision_id           UUID,
  production_id         String,
  revision_id           String,
  scene_id              String,
  entity_ids            Array(String),
  decision_type         LowCardinality(String),  -- camera|location|casting|design|story|schedule
  selected_option       String,
  alternatives          Array(String),
  reason                String,                  -- the "why". Mandatory. Never null.
  cause_tag             LowCardinality(String),  -- taste|constraint|experiment|external_note
  decided_by            String,
  decided_at            DateTime,
  status                LowCardinality(String),  -- active|superseded|reverted
  intentional_deviation UInt8,
  deviation_reason      String
) ENGINE = MergeTree
ORDER BY (production_id, scene_id, decided_at);
```

`cause_tag` is one tap, four options, no typing. It is the difference between a pattern and
a superstition — a rejected handheld shot might be taste, or it might be that the Steadicam
operator had wrapped. Only ever reason from `taste`.

### 4.3 Commitment state — ClickHouse

**This is the differentiator.** Filmustage and Directure know what a scene *contains*.
Neither knows what has already been *paid for*.

```sql
CREATE TABLE commitments (
  commitment_id UUID,
  production_id String,
  entity_id     String,
  scene_id      String,
  state         LowCardinality(String),  -- none|planned|sourced|built|cast|scouted|permitted|shot
  cost_band     LowCardinality(String),  -- none|low|medium|high
  committed_at  DateTime,
  notes         String
) ENGINE = ReplacingMergeTree
ORDER BY (production_id, entity_id, scene_id);
```

Ranking order, highest risk first:
`shot` → `built` / `cast` → `permitted` / `scouted` → `sourced` → `planned` → `none`

### 4.4 Finding output — fixed JSON schema

Determinism matters more than prose here. The model fills this; it does not compose
paragraphs.

```json
{
  "scene": "24",
  "shoot_date": "2026-09-04",
  "severity": "high",
  "commitment_state": "shot",
  "claim": "Dialogue refers to the letter. RANA does not learn of it until scene 31.",
  "evidence": [
    { "type": "scene_line", "scene": "24", "line": 388, "quote": "..." },
    { "type": "scene_line", "scene": "31", "line": 412, "quote": "..." },
    { "type": "decision", "decision_id": "…", "reason": "Withhold to protect the act-two reveal", "decided_at": "2026-08-12" }
  ],
  "suggested_action": "Confirm intentional, or cut the reference in 24."
}
```

**Rule: no finding without at least one evidence object.** If the model cannot cite, the
finding is dropped. This single rule is most of our credibility.

---

## 5. Stack and hackathon compliance

| Requirement | How we satisfy it | Status |
|---|---|---|
| Gemini | Reasoning layer for Extractor and Gate | ☐ |
| Google Cloud | Hosting, storage, runtime | ☐ |
| Agent Builder / Gemini Enterprise Agent Platform | Agent runtime and orchestration — **mandatory**, verify current API surface yourself | ☐ |
| ClickHouse MCP server **at runtime** | Ledger + commitments read/written through it | ☐ |
| Public repo, OSI licence | Detectable at the top of the repo page, in the About section | ☐ |
| **Run instructions in the README** | Graded — the repo must contain "all instructions needed to run". Ours is currently a stub | ☐ |
| Hosted project URL | Live and working | ☐ |
| Demo video | ≤3 min, English or subtitled, YouTube/Vimeo | ☐ |
| New work, created during the contest period | Commit history starts now | ☐ |
| Team ≤ 4 | — | ☐ |

### The runtime rule, verbatim

> Code "must demonstrate the use of Google Cloud and the Partner services **at runtime in
> your code — imported and actually called** (a library import, an app/backend entry point,
> or a loaded agent/flow/MCP config), not just named in the README."

This is mechanically checkable and our track is judged by two ClickHouse engineers — **Gil
Raphaelli** (Director of Engineering, AI/ML) and **Dustin Healy** (Full Stack, AI/ML).
Assume the repo gets read, not skimmed. Two consequences:

- Every ClickHouse call goes through the MCP server. No direct-driver shortcut, not even
  "temporarily".
- **The SQL must be real analytical work** — aggregation over the decision history, joins
  against commitment state, ranking. A `WHERE id = ?` lookup put on screen in front of these
  two judges is worse than showing no SQL at all.

### Judging criteria — four, equally weighted

| Criterion | Wording that matters | What we do about it |
|---|---|---|
| Technological Implementation | "how effectively does it use Google Cloud and the Partner services" | The visible pipeline and the ClickHouse query on screen |
| Design | "a complete, coherent product experience **not just a technical proof of concept**" | A full quarter of the score, and the one teams forfeit. Budget day 8 entirely |
| Potential Impact | "does the solution actually address it **based on what's demonstrated**" | Judged on what's shown, not claimed. The verifiable catch is the whole argument |
| Quality of the Idea | "does the team show **genuine understanding of the problem space**" | Spend the domain research: say "goldenrod pages", name the script coordinator, explain commitment state |

### Free points: IAM and governance

One of the three builder roles the hackathon names is *"the Studio Head enforcing Cloud IAM
security and governance across multi-agent workflows."* We handle unreleased scripts, so
role-scoped access is natural rather than bolted on, and almost nobody else will do it.

Implement it minimally and show it in one screen: props sees only prop findings, the full
script is access-controlled, every ledger write is attributed. Roughly half a day, and it
lands on both Technological Implementation and Quality of the Idea.

---

## 6. Ten-day plan

| Day | Work |
|---|---|
| **1** | Pick a public-domain screenplay. Author a revision that breaks a **second-order reference** — something later in the script that depends on the changed fact. **Hand-write the answer key.** You cannot evaluate what you have not defined. |
| **2–3** | Extractor: script → graph. Fixed schema, structured output, one pass per scene. Do not start anything else until a scene reliably yields facts and knowledge state. This is the hard part. |
| **4** | ClickHouse: schemas, seed decisions with real reasons, read/write through the MCP server. Track requirement satisfied. |
| **5** | Draft diff (fact-level, not text) + commitment state. Keep both boring and legible. |
| **6** | The Gate. Call sheet in, ranked findings out, every line cited. **Tune for precision** — three false positives kills adoption, one missed catch does not. |
| **7** | Deploy on Agent Platform. Wire the intentional-deviation write-back. Add role-scoped access (IAM) — half a day, real points. Budget the rest for runtime, auth and deploy; do not leave this to the last day. |
| **8** | Interface. The call sheet with findings attached *is* the UI. Make it look like production paperwork, not a chatbot. Design is a full quarter of the score. |
| **9** | Video, repo, licence, hosted URL, **README run instructions**. Rehearse the run so the demo never waits on a model. |
| **10** | **Submit Tuesday morning.** Devpost allows edits until the deadline; a submitted entry cannot be lost to a last-hour failure. Remember the deadline lands at midnight Cairo on the night of Wed 9 — there is no day on the 10th. |

### Before day 3 — the two-hour experiment

Sign up for **Directure AI** and **Filmustage**. Feed each one a revision that breaks a
second-order reference. **If either catches it and explains why, stop and rethink.** This is
worth more than any further research, and it is cheap to run now and expensive to discover
on day 9.

---

## 7. Demo video (3:00)

> **Read the rule before storyboarding anything.** The requirement is *"a demo video showing
> your project/agent functioning as built — **not a cinematic trailer**."* Given the
> hackathon is called Agentic Cinema, many teams will produce a beautiful trailer and lose
> points for it. **This is a screen recording of working software** with a spoken voiceover.
> No stylised montage, no title cards, no split-screen staging.
>
> Also: **only the first 3 minutes are evaluated.** Anything after 3:00 does not exist —
> never put the architecture frame at 2:55.

| Time | Beat — all of it on screen, in the product |
|---|---|
| **0:00–0:15** | Cold open on the running app: tomorrow's call sheet, six scenes. Voiceover: *"Six scenes shoot tomorrow. A revision landed Tuesday. Nobody has checked whether it broke any of them — because nobody's job is to check."* No graphics, just the screen. |
| **0:15–0:40** | Click into the graph: scenes, facts, knowledge state, and the decision ledger with reasons. Say it: *"this is the only system that stores why."* |
| **0:40–1:15** | The gate fires on the call sheet. **Nobody presses anything.** Pipeline steps render as they complete. **Put the ClickHouse query and its returned rows on screen** — real aggregation, not a lookup. This frame is most of the Technological Implementation score. |
| **1:15–2:00** | Findings, **ranked by commitment cost, not by count**. Top one is already shot. Every line cites a scene, a line, or a logged decision. Say out loud: *the ranking is the product.* |
| **2:00–2:30** | Mark one intentional. Re-run the identical check: flag gone, stored as evidence with its reason, a related flag resolves differently. Learning a judge can watch happen. |
| **2:30–2:45** | Role-scoped access: log in as props, see only prop findings; the full script is gated. Ten seconds, and it answers the governance half of the brief. |
| **2:45–3:00** | Architecture in one frame — Gemini, Agent Platform, ClickHouse via MCP. Close: *"it runs every night, on a document the production office already makes, before anyone has spent anything."* Stop. No roadmap. |

**Never show:** a chat window as the main surface · any confidence percentage · a director
typing between takes · a breakdown or stripboard screen · a trivial SQL lookup · more than
one feature.

**Say at least once**, because Quality of the Idea rewards domain understanding: *goldenrod
pages*, *script coordinator*, *commitment state*.

---

## 8. Acceptance criteria

The build is done when all of these are true:

- [ ] A public-domain screenplay ingests to a graph with ≥90% of scenes yielding at least one fact
- [ ] The planted second-order break is caught, with correct citations, on the first run
- [ ] **Zero findings without evidence objects.** Enforced in code, not by prompt
- [ ] Findings are ordered by commitment state, and the order visibly changes when a commitment state changes
- [ ] Marking a finding intentional silences it permanently and writes a row to the ledger
- [ ] The re-run after marking produces a demonstrably different result
- [ ] Every ClickHouse read and write goes through the MCP server at runtime
- [ ] Precision on the hand-written answer key ≥ 80%. Recall is secondary — **we would rather miss one than cry wolf three times**

---

## 9. Claims discipline

Things we may say, and things we may not. This matters more than it sounds: one unsupported
claim in front of a judge who knows the industry costs more than a missing feature.

**Do not say:**
- *"We prevent reshoots."* The expensive ones are decided in the edit. *Rogue One* reshot
  ~40% after poor test screenings; *World War Z* rewrote its third act. No dependency graph
  tells you the third act is boring.
- *"Continuity errors cost the industry $X."* No reliable figure exists. Anyone quoting one
  is guessing, and the stat-farm sites offering one are AI-generated.
- *"The AI learns how you think."* Unfalsifiable, and we deleted the feature.

**Do say:**
- *"Late changes are expensive enough that studios now budget reshoot weeks into tentpoles
  in advance."* — Variety, on Justice League's $25M reshoots.
- *"We shorten the distance between a change and the discovery of its consequences from days
  to minutes — before the shoot day rather than after."* Provable on stage in three minutes.
- *"We are not another production suite. We are the check that runs over the ones a
  production already uses."*

---

## 10. Open questions

- Where does commitment state actually come from on a real production? For the demo we seed
  it; for a pilot, it likely comes from the schedule plus department status. Needs a real
  1st AD to answer.
- Is the call sheet the right trigger on episodic, or is it the table read / prep meeting?
- Precision under real revision volume is unknown. The hand-written answer key is one
  script — treat the number it produces as directional, not as a metric.
