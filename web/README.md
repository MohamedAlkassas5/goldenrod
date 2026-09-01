# web

**The call sheet, with findings attached.** That is the whole interface — not a dashboard
with the call sheet in it. `SPEC.md` §5: Design is a full quarter of the score and the
quarter most teams forfeit.

Three files, no framework, no build step: `index.html`, `style.css`, `app.js`. Served by
[`services/api`](../services/api/README.md) at `/`.

```bash
pip install -e ".[api]"
python -m services.api
```

## The design brief, in one sentence

This must look like production paperwork. A 1st AD reads forty call sheets a shoot;
anything that looks like a SaaS product reads as one more thing to learn on a day that
has no room for one.

So the page is **goldenrod paper** — literally what a goldenrod revision is, the WGA
colour issued for production-driven changes. Ink rather than brand colour. Hairline rules
and boxes rather than cards and shadows. Uppercase micro-labels with letterspacing, the way
a real sheet heads its columns. Courier for anything quoted out of the script, because that
is what a screenplay is set in.

Deliberately absent: rounded corners, gradients, drop shadows, and **a colour per scene**.
The last one matters most — a grid of coloured scene strips is a stripboard, and `CLAUDE.md`
is explicit that if a screen starts to look like one we are being compared to Movie Magic
and we lose that comparison.

## What is on the page

| Region | Why it is there |
|---|---|
| Masthead + revision strip | which day, which revision, what it is being diffed against |
| Identity + access strip | who is reading, and what their role is not being shown |
| Pipeline | six steps lighting up as each one actually completes. Click one to see its query |
| Scheduled tomorrow | the day's scenes, each marked clear or flagged |
| Broken, but not on tomorrow's sheet | the revision reaches past the day. One of these is already shot |
| What this revision broke | findings, ranked by commitment state, every claim carrying its citations |
| ClickHouse panel | the query **and the rows it returned**, side by side |
| What the office has logged | the ledger with its reasons, knowledge state, facts, commitment state, and who holds access |

## Two rules the JavaScript must not break

Both from `CLAUDE.md`, both enforced by `tests/test_api.py`:

1. **No confidence percentages. Ever.** Raw counts or nothing.
2. **Nothing is rendered that the API did not return.** No client-side severity, no
   client-side ordering by anything but the server's own rank, no invented text on a
   finding. The ranking is the product; it is computed by the `commitmentRank` UDF in
   ClickHouse and the page only draws the order it was given.

The vocabulary is checked too: *production office*, *departments*, *governance* — never
*filmmakers* or *creative partner*.

## Role-scoped access, on the page

The identity switcher in the masthead is the local stand-in for the platform's account
picker; behind Cloud IAP it does not render at all. Choosing someone sets a same-origin
cookie and re-runs the check from scratch — **it changes a cookie, never a filter.** All
the scoping happens on the server, so a curl command with the same cookie sees exactly
what the page does, and one without sees a 401.

What a scoped role gets:

- a strip under the revision line naming the role, the departments it covers, how many
  findings were withheld and whether pages were withheld
- findings it owns, with every verbatim script line replaced by a redaction marker — the
  scene and line survive, so the finding still cites
- pipeline steps with their SQL and **not** the rows those queries returned
- *Office sign-off required* where the office sees *Mark intentional*
- the ledger, facts and knowledge-state tabs replaced with a line saying so, rather than
  an empty list that would read as "the office has logged nothing"

Nothing is hidden quietly. A page that silently drops a finding is worse than one that
says it dropped it.

## Self-contained

No CDN, no webfont, no npm. A demo that needs the network on the day is a demo that fails
on the day, and a test asserts the page loads nothing external.
