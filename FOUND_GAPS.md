# Found Gaps

A running log of gaps found while building — what surfaced, how, and what was done
about it. Kept because the interesting part of a build is rarely the happy path, and
because §7 of [REPORT.md](REPORT.md) has to be honest about what was and wasn't
covered.

**Read the class column carefully.** Most entries here are *coverage* gaps: the code
was already correct, but nothing proved it. Those are not bug fixes and should never
be written up as such.

| Class | Meaning |
|---|---|
| `coverage` | Behaviour was already correct; nothing verified it. Test added, no code change. |
| `defect` | Behaviour was wrong. Code changed. |
| `design` | A hole in the approach, caught before it could bite. |
| `environment` | Not the code — the machine, the tooling, the process model. |

---

## Day 1 — target app (DESIGN §2)

### G-01 — Region scope untested on the mutation route
**Class:** `coverage` · **Found:** converting the smoke script to pytest

Region denial was only ever exercised against `/member/<id>`. The sub-account routes
enforce it too ([app.py](target_app/app.py), `subaccount_new` / `subaccount_confirm`),
but no test said so.

Why it matters: region scope has to be a **per-request rule, not a profile-page
decoration**. If it were only enforced on the profile, an agent — or anyone with the
URL — could skip straight to `/member/67890/sub-account/new` and mutate a record it
was never allowed to read. The guard existed; the guarantee didn't.

**Resolution:** `test_denial_also_covers_the_mutation_route`. Passed on first run —
no behaviour changed.

---

### G-02 — Confirm route revalidation untested
**Class:** `coverage` · **Found:** converting the smoke script to pytest

The confirmation screen carries its state in hidden inputs, so `POST
/sub-account/confirm` is stateless. That is a deliberate design choice — it keeps the
irreversible step to exactly one unambiguous submit — but it means the final POST is
**fully attacker-controllable**. `_validate_form` already ran on it; nothing tested
that it did.

Why it matters: this is the step §8 classifies as `irreversible` and gates behind
human confirmation. A tampered POST with a $1 deposit reaching the "Sub-account
opened" page would mean the risk gate protects a step that can be walked around.

**Resolution:** `test_confirm_route_revalidates`. Passed on first run — no behaviour
changed.

---

### G-03 — Seed data trusted rather than asserted
**Class:** `coverage` · **Found:** converting the smoke script to pytest

§2 defines region as a function of the ID range (`10000–49999` Eastern,
`50000–89999` Western). The ten seeded members each carry an explicit `region` field.
Two sources of truth, nothing reconciling them.

Why it matters: a typo'd seed ID — `57890` instead of `67890` — creates a member
whose stored region contradicts the range rule. Every test using that member keeps
passing, and the contradiction only surfaces later as an inexplicable
`PERMISSION_DENIED` in a replay run, where it looks like an engine bug.

**Resolution:** `test_seed_data_agrees_with_the_id_range_rule` walks all ten members
and asserts `region_for_id(id) == member["region"]`. Plus
`test_every_demo_id_behaves_as_the_evidence_set_expects`, pinning the four IDs the
§10 evidence set depends on.

---

### G-04 — Sub-resources would have swallowed armed chaos
**Class:** `design` · **Found:** while writing `chaos.py`, before it could bite

Chaos flags arm on the request carrying `?chaos=`, then fire on the *next* request.
The naive implementation fires on the next request of **any kind** — and a browser
does not politely wait. Chromium fetches `/favicon.ico` on its own, and the member
profile immediately pulls `/member/<id>/loan-frame` into its iframe.

Why it matters: the armed flag would be consumed by a favicon fetch and the operator
would never see the condition. Worse, it would be *intermittent* — dependent on
browser cache state and request ordering — which is the kind of flake that eats an
afternoon during a demo. It would have broken evidence item 5 in §10 (chaos=dialog →
NEEDS_INTERVENTION → handoff → resume) non-deterministically.

**Resolution:** `CHAOS_ELIGIBLE_ENDPOINTS` in [chaos.py](target_app/chaos.py) — only
top-level page endpoints arm or fire. Locked in by
`test_sub_resources_do_not_swallow_an_armed_flag`, which explicitly loads the favicon
and the iframe between arming and firing.

---

### G-05 — A stale server kept serving pre-rename templates
**Class:** `environment` · **Found:** the rename appeared not to take effect

After renaming the portal, the browser still showed the old name through a terminal
restart. Two compounding causes, neither in the app:

1. **`python -m target_app` spawns two processes.** The venv's `python.exe` re-execs
   the base interpreter (`pyvenv.cfg` → `home = D:\Python`), and the *child* owns the
   socket. Closing the terminal doesn't reliably take the child with it. A server
   started hours earlier was still alive and still holding port 5000.
2. **Windows doesn't error on the port collision.** Werkzeug sets `SO_REUSEADDR`,
   which on Windows *permits* a second bind to an already-bound port rather than
   refusing it. The new server printed `Running on http://127.0.0.1:5000` and looked
   perfectly healthy while the old process answered the requests.

Combined: a restart that silently does nothing, with no error anywhere.

Why it matters beyond the annoyance: during Day 3–4 replay demos this presents as an
artifact "drifting" or a checkpoint mysteriously failing, and the instinct will be to
debug the engine. It is worth suspecting the process table first.

**Resolution:** no code change — this is operational. Diagnostic recorded here:

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*target_app*' } |
  Stop-Process -Force
```

Partly mitigated by `TEMPLATES_AUTO_RELOAD = True`, which removes the need to restart
for template edits at all — so the trap is sprung far less often.

---

## Day 2 — executor (DESIGN §3, §8)

### G-06 — Controls were labelled by their own content
**Class:** `defect` · **Found:** first run of the executor tests

`labelOf` returned an element's own text before consulting the adjacent cell. For
a `<select>` the text content is its *option list*, so the branch picker came back
named `E14 — Eastern Main` instead of `Branch`.

Why it matters: the label is the first and most durable strategy in the fallback
chain. A wrong label is worse than a missing one — it records confidently, replays
against a value that changes when someone picks a different option, and the failure
surfaces months later as an unresolvable target.

**Resolution:** own text now names only links and buttons, the elements whose
visible content *is* their name. For every other control the content is data.
`test_login_controls_are_named_by_their_adjacent_cells`.

---

### G-07 — `read` worked by accident
**Class:** `defect` · **Found:** while fixing G-06

Label cells and value cells were collected as separate elements, so an observation
contained `cell 'Member name' = 'Member name'` and `cell 'Alice Torres' =
'Alice Torres'`. The model could see a field name and a value with nothing
connecting them. The `read` test passed only because `_locate_by_label`
independently walked to the following sibling at resolve time — the element's own
structural path pointed at the label cell, so label and structural resolved to two
*different* nodes.

Why it matters: the fallback chain assumes every strategy targets the same thing.
Here, a label hit would read the balance and a structural fallback would read the
word "Savings balance" — a silent wrong answer rather than a failure, which is the
worst class of bug this system can have.

**Resolution:** text cells are walked a row at a time; a label/value pair collapses
into one element that *is* the value, carries the neighbour's text as its label,
and whose structural path points at the data cell. Standalone cells (error banners,
headings) are still recorded on their own terms, which is how "No member matches
this number" reaches the agent.

---

### G-08 — `<form>` is hoisted out of table structure
**Class:** `design` · **Found:** a test selector matched nothing

`document.querySelector('form tr')` returns null on the search page. The HTML
parser reparents a `<form>` that appears between `<table>` and `<tr>`, so the form
element ends up containing no rows at all. Submission still works — the parser sets
the form owner on the controls regardless — but the DOM does not look like the
source.

Why it matters: it is direct evidence for the design's central perception bet. The
markup you read and the tree you get are *different*, so a structural path derived
from either is guesswork. This is the case for label-first targeting, made by the
surface itself rather than argued in the abstract. Worth a sentence in REPORT.md.

**Resolution:** none needed — the executor never relied on it. The test now reaches
the row through the input. Recorded because it is the kind of thing that reads as a
bug in the app when it is really a property of HTML.

---

### G-09 — Context pruning silently kept the first screenshot
**Class:** `defect` · **Found:** by the fake-client loop tests, before any live run

Screenshots live at two depths in the message list: directly in a message's
content (the opening message) and nested inside a `tool_result` (every turn after).
The pruner handled the nested case but, for the top-level one, built a throwaway
one-item list and mutated *that* — so the opening screenshot was counted against
the budget and then left in place on every subsequent request.

Why it matters: not correctness, cost. A full-page PNG re-sent on all 25 turns of a
discovery run is the single largest avoidable line in the bill, and the failure is
invisible — the run works perfectly, just expensively. The kind of thing that is
never noticed without a test that counts.

**Resolution:** both depths walked, each list mutated in place.
`test_old_screenshots_are_dropped_from_context`.

The same test was also over-counting: it held a reference to the loop's live
`messages` list, so a screenshot appended *after* a request appeared to have been
sent on it. The fake client now deep-copies each request, which is what a request
record should have been in the first place.

---

### G-10 — The first real discovery run did the task and then failed
**Class:** `design` · **Found:** the first genuine LLM-driven run (`run_20260813_232018`)

Eight steps, all correct: navigated, typed both credentials as `{secrets.*}` tokens,
signed in, searched member 12345, read `$4,523.18`. Then `done` carried:

> `"Savings balance $4,523.18 shown on Member Profile page for Record 12345 /
> status ACTIVE, Member name Alice Torres"`

as its `success_evidence`. No such string exists on the page — it is a *description*
assembled from four separate cells. The literal check correctly returned false and
the whole run was recorded as `HARD_FAILURE`.

Why it matters, three ways:

1. **The check was right and must stay.** That evidence string becomes the
   artifact's permanent `success.checkpoint`. Accepting prose would have produced a
   capability whose success condition can never match at replay — a broken artifact
   that looks fine on the day it is recorded.
2. **The prompt was wrong.** It asked for "exact text visible on the final page",
   which a model reads as a description of the requirement rather than a format.
   The instruction now shows a good and a bad example, and names the exact failure:
   *quote, do not summarise*.
3. **The response was too harsh.** Discarding seven correct steps — and every model
   call that produced them — over the formatting of one field is a waste. A rejected
   claim is now a recoverable condition: the agent is told what was wrong and gets a
   bounded number of attempts to quote real text. The guarantee is unchanged, since
   only a literal match still succeeds.

A fourth, smaller cause sat underneath: `text_present` compared raw `inner_text`
against the needle, but the element list the agent quotes from is
whitespace-normalised, and `inner_text` puts newlines between table cells. A phrase
copied faithfully out of the observation could fail to match the page it came from.
Both sides are now normalised — still a literal substring test, just not a test of
the DOM's whitespace habits.

**Resolution:** `test_unverified_success_claim_is_rejected_until_it_is_quoted`,
`test_a_claim_that_stays_unquotable_fails_the_run`.

---

### G-11 — The first artifact could only ever succeed for the member it recorded
**Class:** `defect` · **Found:** reading the first distilled capability

`run_20260814_001015` produced a valid, schema-passing artifact whose success
checkpoint was:

```json
"success": { "checkpoint": { "condition": "text_present", "value": "$4,523.18" } }
```

That is member 12345's balance. Replaying with member 23456 shows `$18,240.55`, the
checkpoint fails, and the run never reports SUCCESS — so the capability works only
for the run that created it. That is DESIGN §10's demo #2 ("replay member 23456,
*different from recorded*") failing by construction.

The agent did nothing wrong. The prompt told it to:

```
Good:  "$4,523.18"   (a value, exactly as shown)
```

That example was added while fixing G-10, where the failure was quoting *prose*
instead of page text. The fix was correct and introduced a worse bug: a balance is
a perfect quotation and a useless checkpoint. **Fixing the presence problem created
a stability problem.**

There is a second edge to it. Balances are masked in the step trace under §8 — and
then one was written into `capabilities/lookup_member_balance.json`, which is
committed. The redaction hole reopened one file downstream of where it was closed.

Why the mechanism mattered more than the wording: a value passes a presence check
*perfectly*. Nothing about the artifact looks wrong. It fails months later, on a
different input, with no obvious cause.

**Resolution:** three parts.
- The loop now tracks every value that varies with the inputs — anything `read`
  from the page, and anything typed that came from the goal — and refuses success
  evidence containing any of them, whatever the page says. Same correction budget
  as G-10, a distinct rejection reason.
- The prompt inverts its example: `"$4,523.18"` is now the worked *counter*-example,
  with the test to apply — *would this text be on screen for a different member?*
- The distiller applies the same rule to per-step checkpoints: a URL fallback
  recorded as `/member/12345` had the identical defect and is now templated to
  `/member/{inputs.member_number}`. The contract validator learned to check
  templates inside checkpoints, which it previously ignored.

**Also fixed in the same pass:** the capability description read *"Recorded from
the goal: look up member 12345…"*, telling a calling agent this capability looks up
member 12345 — which is precisely what it stopped doing when the literal became a
parameter. Literals that become inputs are now substituted out of descriptions and
step reasons, with a whole-file test asserting the recorded member number appears
nowhere in the artifact.

**And:** `DEPOSIT_BELOW_MINIMUM` was attached to a read-only lookup that cannot
reach it. Declared outcomes now carry a `routes` scope and only those intersecting
the flow's visited routes are attached — otherwise replay scans for unreachable
text after every step of every capability, forever.

---

## Day 3 — replay engine (DESIGN §6, §7)

### G-12 — A timed-out step vanished from the trace
**Class:** `defect` · **Found:** first full run of the replay suite

`_execute` wrote its `step` event *after* the action returned, so an `ActionTimeout`
propagated straight past the record. Under `--chaos slow` the trace read
`[1, 2, 3, 5, 6, 7]`: the one step that actually struggled was the one step with no
entry.

Why it matters: "a step ran exactly once" is the assertion that proves a timed-out
action was re-checked rather than re-sent. Without the record the guarantee is
unfalsifiable at exactly the moment re-sending is the tempting mistake — a submitted
form processing while the browser gave up waiting.

**Resolution:** the action is wrapped, the event is written either way carrying
`timed_out`, and the exception re-raised afterwards.
`test_a_timed_out_action_is_re_checked_never_re_sent`.

---

### G-13 — A recovered slow load recorded nothing
**Class:** `defect` · **Found:** same run, adjacent assertion

`_verify` logged a `SLOW_LOAD` recovery only when its retry loop needed a second
attempt. But after the click timed out at 5s, the first thing `_settle` does —
reading page text — blocks on the in-flight navigation and returns at ~8s with the
page already there. Re-check attempt 1 passed, so the loop concluded nothing had gone
wrong. An 8s stall recovered **silently**.

Why it matters: the run was correct and the evidence was wrong, which is the failure
that survives a demo and bites in a postmortem. It is also precisely what
`runtime.yaml` argues for in its own comments — short waits with retries "tell you it
was late and by how much", which they only do if lateness is recorded.

**Resolution:** the action's own bounded wait counts as the first attempt, so the
first successful re-check logs `attempts: 2`. The recovery reads `re-checked` after a
timeout and `retried` after pure polling — §6's never-re-send rule visible in the
trace rather than only in prose.

---

### G-14 — A test asserted the engine default over the shipped config
**Class:** `coverage` · **Found:** same run

`test_engine_defaults_apply` asserted `step_timeout_ms == 10_000`, the dataclass
fallback, while `runtime.yaml` declares `5000` with a paragraph explaining why. The
test had been written before the config grew the key.

Why it matters: the naive fix is to change the config to match the test, and that
would have been quietly destructive. The 8s chaos stall has to *exceed* the
per-attempt bound or `SLOW_LOAD` becomes unreachable — one long wait absorbs it, the
run goes green, and an entire class of tests passes while testing nothing.

**Resolution:** the assertion now pins `5_000` with the reason attached, plus
`test_an_unset_timeout_falls_back_to_the_engine_default` so the `10_000` default
still has a test that reaches it.

---

### G-15 — The only recoverable dialog path had no test
**Class:** `coverage` · **Found:** auditing §6's five conditions against the suite

`KNOWN_INTERSTITIAL` is the one runtime condition where the engine *clicks something*
on a live page. `runtime.yaml` leaves `known` deliberately empty — the maintenance
notice must stay unknown or §10's handoff demo is undemonstrable — so the dismissal
branch had never executed against a browser. Only the string match was unit-tested.

Why it matters: an untested branch that clicks a button on a banking screen, reached
only when something has already gone wrong.

**Resolution:** `test_a_declared_interstitial_is_dismissed_and_the_run_continues`
supplies the declaration in the test rather than the config. That is also the better
test: it pairs against `test_an_unknown_dialog_stops_and_hands_over` as the same modal
on the same page with opposite outcomes, proving the engine's rule is structural and
the judgement lives in config.

---

### G-16 — `settle_timeout_ms` was a dead knob
**Class:** `defect` · **Found:** reading the config loader beside the surface

`RuntimeConfig` parsed `settle_timeout_ms` and nothing ever read it; `settle()` used a
module constant. Both were `5000`, so nothing misbehaved.

Why it matters: a knob that silently does nothing is worse than no knob. Someone
raises it during an incident, sees no change, and concludes the waiting policy is
broken rather than the wiring.

**Resolution:** carried through `set_timeout(ms, settle_ms)` from the engine. The
regression test is the end-to-end one asserting the configured value reached the
surface — the unit tests would have passed throughout the period it was dead.

---

### G-17 — Chromium refuses ports the OS is happy to hand out
**Class:** `environment` · **Found:** an entire browser suite failing at once

`live_server` binds to port 0. One run drew **1720**, which is on Chromium's
restricted-port list (H.323), and every browser test died on the first `page.goto`
with `net::ERR_UNSAFE_PORT` — before a request left the browser. The server was fine;
the port was unspeakable.

Why it matters: rare enough to read as flakiness, total enough to read as a
regression, and the error text points at neither. Same family as G-05 — the machine,
not the code — and the same instinct applies: suspect the environment before the
engine.

**Resolution:** the fixture redraws until it lands on a port Chromium will talk to,
with the restricted list inline and the reason next to it. Port 0 stays, because
G-05's stale-server hazard is still real.

---

## Day 4 — human-in-the-loop (DESIGN §9)

### G-18 — A pause was traced under two different names
**Class:** `defect` · **Found:** first run of the 4a tests

`paused_for_human` was emitted inside `_intervention`, which only the risk-gate path
calls. Dialog and session pauses built their own payload in `_settle` and emitted
`unknown_dialog` and nothing else. Two triggers, the same event, and the name you
would grep for was missing on two of the three paths.

Why it matters: the handoff worked end to end in that very test — dialog stopped the
run, operator dismissed it, run resumed and finished — and the trace could not answer
"did this run ever stop and wait". Right behaviour, wrong evidence, in the feature
whose entire purpose is auditability.

**Resolution:** traced in `_offer`, which every trigger funnels through, emitted even
when there is no console to answer, and carrying `when` so the trace ties to the
resume semantics.

---

### G-19 — The record assumed one handoff per run
**Class:** `design` · **Found:** implementing 4b against the Day 1 schema

`intervention_record` had been in `result.schema.json` since Day 1 with
`paused_at_step` and one `post_resume_checkpoint` — a single entry. `open_sub_account`
has both a `mutating` and an `irreversible` step, so an unattended invocation pauses
**twice**, and a blind-resume loop pauses three times on one step.

Why it matters: the entry that would have been dropped is a human approving a write to
a member's account, which is the one an audit exists to keep.

**Resolution:** DESIGN §7 amended first (per its own rule about contracts), then the
schema: a per-run `operator` and an `interventions` array, with `when` and `resolution`
required on each. A pre-existing contract test pinned the old shape and failed —
correctly — and was updated to a realistic record rather than merely made to pass.

---

### G-20 — The console told the operator to do the thing it was asking permission for
**Class:** `defect` · **Found:** the first human-driven run of the seam

The banner said *"Act in this window, then return to the terminal"* and the request
said *"Confirm this specific action"*. Both are correct for an `after_step` pause,
where an unknown dialog genuinely needs a person to act. At a risk gate they are
exactly backwards: the operator is being asked for **permission**, and the automation
does the clicking.

So the operator did what the interface said, pressed the application's own confirm
button, and the run resumed into a page where its recorded target no longer existed —
`HARD_FAILURE` on a step that had in fact succeeded.

Why it matters three ways:

1. **The distinction existed everywhere except where it mattered.** `before_step` and
   `after_step` were already in the engine, the trace and the schema; both human-facing
   channels said the same thing regardless.
2. **Wording is mitigation, not a guarantee.** With the corrected wording in front of
   them — banner and terminal both saying *do not do this yourself* — the operator
   clicked the button anyway on the very next run. That is what a person does when a
   confirm button is on screen and they have just been asked to confirm. The engine has
   to be safe regardless of what anyone reads.
3. **A re-send here is unrepeatable.** This is the `irreversible` step. Clicking it
   twice is a second sub-account on a member's record.

**Resolution:** three parts.
- Both channels branch on `when`, down to the button labels (`[r] approve and run this
  step` versus `[r] resume`), and the gate messages now say who does the clicking.
- `HUMAN_PERFORMED_STEP` (documented in DESIGN §9): when a step the operator approved
  then fails to resolve its target, the engine checks that step's own checkpoint — if
  the expected state is already there, the person did it, and the action is logged and
  **not re-sent**. Same rule as a timed-out action from the other direction. Narrow by
  construction: resolution is attempted first, and a step without a checkpoint fails
  loudly rather than guessing.
- The banner carries **Approve / Abandon** buttons, because the root cause is that the
  operator is in a browser and wants to click something. Giving them the right thing to
  click beats telling them not to click the wrong one.

Verified by the run after: the operator pressed the button, the automation found its
target gone, checked the end state, did not re-send, and finished `SUCCESS` with one
sub-account.

---

### G-21 — `verified` would have claimed a check that never happened
**Class:** `design` · **Found:** wiring discovery's `stuck` into the same seam

Replay resolves a handoff as `verified` because it re-checks the paused step's
checkpoint. Discovery has no checkpoint to re-check — the flow is still being
discovered — so reusing `verified` there would have recorded a verification that never
ran, in the document whose purpose is to be trusted after the fact.

**Resolution:** `resumed` added to the enum for "control came back, nothing to
re-check", separate from `verified`, in both the schema and DESIGN §7.

---

### G-22 — Substitution protects a secret at the keystroke, not afterwards
**Class:** `design` · **Found:** reading the committed transcript before submission

§8's redaction story is that the model is handed the token
`{secrets.operator_password}` and the executor substitutes the real value at the
keystroke, so the credential is *absent* from the transcript rather than scrubbed
from it. That holds for the password and, as written, was claimed for both secrets.

It is only true for the password, and for a reason that has nothing to do with
substitution: a password field's value is never read during element distillation,
so it never enters our process at all. The operator ID is typed into an ordinary
textbox, and the next observation reads that box back like any other text input —
so `textbox 'Operator ID' = 'e.okafor'` appears twice in each committed discovery
transcript, while the password appears zero times.

Why it matters beyond the wording: substitution reads like a *general* mechanism
for keeping secrets out of the model's context, and it is not. It protects the
value on the way in. Anything typed into a control whose value can be read is back
in the observation on the next turn, and therefore back in the model's context and
the transcript. The real boundary is the **control type**, which the executor
enforces structurally — not the token, which only defers the exposure by one turn.

The fix is small and known: have the executor keep the set of values it has
substituted this run and mask them out of element-list text before an observation
is built, so any substituted secret is protected for the whole run rather than for
one keystroke.

**Resolution:** documented, not fixed. Masking changes what the model observes, so
it needs a fresh discovery run to be trusted — and re-recording discovery would
produce different artifacts and invalidate the entire frozen evidence set, which is
a worse trade at this point than an accurate sentence. The claim is now stated
precisely in README, `evidence/README.md` and REPORT §6 rather than absolutely.

---

## Still open

Known and deliberately not addressed yet.

### O-01 — A declared build can't detect undeclared drift
**Blocks:** nothing · **Status:** deliberately unbuilt — argued as the top next step
in [REPORT.md](REPORT.md) §7

`app_fingerprint` is scraped from the app's self-reported build
(`legacy-cu-portal@4.3.0`). That catches changes someone *remembered to announce*. A
reordered `<tr>`, a renamed cell, a moved form field — all of which break recorded
locators — leave the build string untouched, and replay finds out by failing.

The complement is a structural hash computed by the executor from observed DOM: the
tag skeleton of the pages a capability touches, text stripped out. Declared version
carries the human-readable story; the hash does the actual detecting.

The reason it kept losing is defensible: the strategy chain is what actually copes
with a moved element, and a failed step already names what it tried and where it
stopped. The hash would improve *when* you learn about drift, not *whether* — worth
real money in production, and worth less than the handoff inside a time box.

### O-03 — Is the pause record forensics, or a resume token?
**Blocks:** nothing · **Raised:** Day 4, before 4a · **Undecided**

§9 step 1 says a pause persists run state, and does not say whether a *second process*
could pick that up and continue. Same-process resume is what is built, and §9's own
requirement points that way — the human operates the same live browser session, so a
second process would have to take over a Playwright connection the first one owns and
is blocked on.

Leaning to forensics-only, with the limit stated in REPORT rather than left looking
like an oversight. Recorded as a TODO in DESIGN §9 because the answer decides what the
pause record must contain.

### O-04 — Sub-account numbers are not unique
**Blocks:** nothing · **Deliberate**

`sub_account_number(member_id, account_type)` is a pure function, so opening two
Holiday Club accounts for one member yields `SA-23456-69` twice. That follows §2's
rule that behaviour is a function of the member ID, which is what makes evidence
reproduce — and it is wrong in the way a real ledger would never be.

Not fixed: the target app is a prop and is evaluated on nothing, and determinism is
worth more here than realism. Worth a line in REPORT §7 as a known simplification, so
it reads as a choice rather than an oversight.
