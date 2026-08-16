# Design write-up

A system that lets an AI agent operate legacy back-office software that offers no
API. An LLM works the task out once against a live surface; the run is compiled
into a typed capability artifact; after that the task executes deterministically
with no model in the decision loop.

**Discovery is a compiler. Replay is a VM. The artifact is the bytecode, and its
schema is the instruction set.** Every decision below follows from taking that
seriously.

[DESIGN.md](DESIGN.md) is the full spec and predates the code. [FOUND_GAPS.md](FOUND_GAPS.md)
is the honest log of what broke on the way, including the two defects that only a
human driving the handoff could have found.

**Every claim below is a recorded run: [`evidence/`](evidence/README.md).**

```bash
python -m cua discover --goal "look up member 12345 and read their savings balance" \
  --target http://127.0.0.1:5000 --save-as lookup_member_balance
python -m cua replay lookup_member_balance --param member_number=23456   # SUCCESS, 18240.55
python -m cua replay lookup_member_balance --param member_number=99999   # MEMBER_NOT_FOUND
```

---

## 1. Architecture

Two modes over one executor.

```
cua/executor/    surface abstraction, element distillation, actions, allowlist
cua/agent/       discovery: observe → decide → act, six tools
cua/distill/     trace → artifact
cua/replay/      the interpreter: binding, targeting, checkpoints, outcomes, recovery
cua/hitl/        pause → cede control → resume, and the operator console
cua/contracts/   JSON Schemas + validator for artifact and result envelope
```

**The executor is shared by discovery and replay, deliberately.** It lives beside
them rather than under either. If the two used different machinery to click, a
capability could pass discovery and fail replay for reasons that have nothing to do
with the application — and every such failure would be charged to the artifact
schema, which is the thing actually under test.

**Nothing above `executor/surface.py` imports Playwright.** That file defines a
small protocol — observe, navigate, click, type, read — plus two dataclasses. The
artifact records *what was targeted and how it was identified*, never which library
did the clicking. That is the seam a desktop surface would slot into (§4).

**Perception is hybrid: a screenshot plus a distilled element list.** Pure DOM
selectors contradict the no-clean-DOM reality; pure screenshot-and-coordinates makes
replay fragile. Recording everything about a target at discovery time is what lets
the artifact carry an ordered fallback chain instead of one guess. Critically the
list is **not only interactive elements** — a savings balance is not clickable, and a
control-only list would push extraction into OCR. A label/value pair in a table row
collapses into one element that *is* the value and carries its neighbour's text as
its label, which is what makes `label: "Savings balance"` a read target.

**Three config files, because there are three kinds of knowledge**, changed for
different reasons by different people: `policy.yaml` is what automation is
*permitted* to do, `outcomes.yaml` is what a business result *means*, `runtime.yaml`
is how to *recognise* a condition on this surface. The sharpest line is between the
last two. A business outcome is capability-specific, declared in the artifact, and
terminal; a runtime condition is app-wide and mostly recoverable. Conflating them is
how "the page was slow" reaches a caller dressed as a business result.

**Trade-offs taken:**

- **Hand-rolled agent loop, not LangGraph.** A single linear agent with one tool
  call per turn; the loop structure is itself under evaluation, and a framework
  would hide it. The threshold where one earns its place: multiple concurrent
  capabilities, or durable multi-agent state.
- **Single process, synchronous Playwright.** No queue, no workers. The handoff
  needs a live browser session a human can take over, which a job queue makes
  materially harder. Cost: no concurrency. Buying that back means a session-owning
  worker process, which is real work and not what this assignment is testing.
- **Evidence is a module, not logging.** One directory per run, the same trace
  format for both modes, so a reviewer comparing a discovery run with a replay of
  the artifact it produced reads two files with the same shape.

---

## 2. Artifact schema

One JSON file per capability, validated on save *and* on load. Reference shape in
[DESIGN.md §5](DESIGN.md); the load-bearing choices:

**`target.strategies` — an ordered fallback chain, not a selector.** `label` (role
plus visible or adjacent text) first, because on a legacy surface the text beside a
field outlives a table reshuffle. Then `structural`, exact today and brittle to
layout edits. Then `coordinates`, which never travel without `verify_text_nearby` —
never click blind. Replay tries them in order and logs which won, which is drift
telemetry for free: you can see which tier is carrying the weight before it fails.

**`expected_outcomes` are declared in the contract, not discovered at runtime.**
This is what keeps "no such member" from ever being a crash. The caller branches on
`MEMBER_NOT_FOUND`; the engine never has to decide whether an unfamiliar page is a
failure or an answer.

**Per-step `risk` and per-step `checkpoint`.** The policy gate hangs off every step,
and "the click worked" is verified rather than assumed — so a failure is attributed
to the step that caused it instead of surfacing three steps later as an unresolvable
target.

**Extraction lives on the step, not at the end.** A `read` step names the output it
fills. The alternative — a `success.extract` block after the walk — cannot work: by
the last step the page holding the value is usually gone. `outputs.*.source_step`
back-references the producing step and the validator enforces that correspondence,
because JSON Schema cannot express it.

**`{secrets.*}` and `requires_secrets` make redaction structural.** A capability
declares which credentials it needs *by name*; values resolve from the environment at
run time. An artifact that stored a password would have to store it somewhere, and
there is nowhere.

**`app_fingerprint` is scraped from the application, never authored by us.** A
fingerprint we write cannot mismatch, and a check that cannot fail is decoration.
`app` is the stable routing key; `app@build` is expected to change.

**Input names are derived from on-screen labels** (`Member number` → `member_number`),
not hand-picked. A curated mapping would read better and would be one more thing to
keep in step with a UI nobody controls.

**Two validation layers.** JSON Schema owns shape, including action-specific rules
that make a half-formed step impossible — a `navigate` with no checkpoint, a
`coordinates` strategy with nothing to verify against, a `read` that does not say
what it fills. The validator owns cross-field facts JSON Schema cannot express. Every
problem is reported at once rather than one per run.

**Versioning:** `schema_version` is the format; `capability.version` is semver of the
recording — patch for a re-record of the same flow, minor for new optional outputs,
major when inputs or outputs change.

---

## 3. Determinism & error handling

**Zero model calls is enforced by the contract, not asserted in prose.** The result
schema constrains `mode: "replay"` to `llm_call_count: 0`, and the count is measured.
A replay result carrying a model call is not a warning — it is an invalid document.

**The recorded chain is executed, never regenerated.** Rebuilding locators from the
live page would quietly repair drift instead of reporting it, and the whole value of
the chain is knowing which tier is holding.

**Three classes, and the ordering between them is the design.** After every step:

1. **Declared business outcome** → terminal status the caller branches on.
2. **Recoverable condition** → handled by policy, logged in the trace, *never* a
   terminal status.
3. **Hard failure** → terminal, with maximum forensics and deliberately no
   remediation advice. If the engine knew the fix it would be a recoverable
   condition, so a suggestion would be a guess wearing the costume of an
   instruction — `additionalProperties: false` makes adding one a schema error.

The outcome scan runs **before** a missing checkpoint is treated as a failure. For
member `99999`, step 6 expects "Member Profile" and does not get it; a
checkpoint-first engine calls that a crash. That single ordering decision is most of
what the three-class rule buys.

**Waiting is executor policy and never model-decided** — the agent has no `wait`
tool, because a model that can wait will paper over a broken step instead of
declaring it stuck. Each attempt is bounded (5s) and the retry policy supplies the
overall tolerance (~18s). Splitting it that way is deliberate: one long wait can only
report that the page eventually arrived, while short waits plus retries report that
it was *late and by how much* — the difference between a run that silently takes
twelve seconds and one that records a recovered `SLOW_LOAD`.

**A timed-out action is re-checked, never re-sent.** A submitted form can be
processing while the browser gives up waiting; re-sending is how automation
double-posts. The same rule appears twice more: on resume after a handoff, and when
an operator performs a step themselves (§5).

Runtime conditions, from `runtime.yaml`: `SLOW_LOAD` retries with backoff;
`SESSION_EXPIRED` is recoverable **only** because a re-login routine is declared for
the app — and that routine is ordinary artifact steps run by this same interpreter,
because a recovery path with its own machinery would be a second, less-tested engine
running exactly when things are already wrong; `KNOWN_INTERSTITIAL` is dismissed and
logged; `UNKNOWN_DIALOG` escalates, never guesses; `APP_ERROR` is a hard failure with
no retry, because a 500 is the app saying it is broken and asking again is not a plan.

**UI drift** is warned about, not failed on: most app changes do not touch the
recorded flow, and the strategy chain is what actually copes. The known limit is that
a declared build only catches changes someone remembered to announce — a reordered
`<tr>` breaks locators and leaves the version string untouched. The complement is a
structural hash over observed DOM, designed and not built (§7).

---

## 4. Heterogeneity & multi-tenant

**Surface abstraction.** The seam is `Surface` plus `Element`: three verbs and an
observation. A desktop surface driven by OS-level automation would implement the same
protocol and produce the same `Element` records, and neither the agent loop nor a
saved artifact would need to change. The strategy chain maps cleanly — `label`
becomes the accessibility name, `structural` becomes the control tree path,
`coordinates` stay coordinates and still refuse to travel without nearby text to
verify. The artifact never learns which library clicked.

The honest limit: `executor/browser.py` is the only implementation, and a second one
would exert real pressure on `distill/` (which reads a browser-shaped trace) before
the abstraction proved itself. Building against one surface and *claiming* two is
exactly the brittle assumption the brief warns about, so it is listed as designed,
not done.

**Multi-tenant reuse** turns on the split already in the schema: `app` is the stable
routing key — which capabilities belong to this application, which recovery routines
apply — while `app_fingerprint` is `app@build` and is *expected* to differ per tenant
and per upgrade. That is the seam per-tenant variants hang from: a base artifact plus
an override layer keyed by fingerprint, overriding only the steps that differ, rather
than re-recording per tenant. Reuse degrades gracefully rather than failing closed
because the strategy chain already tolerates one tier breaking, and drift warns
instead of refusing.

Two pieces would make it real, and neither is built: **canonicalization**, so
`/member/12345` is stored as a pattern rather than a route with a literal in it (the
distiller already templates inputs into checkpoints and URLs, which is the same
machinery pointed at a different problem), and **per-variant override resolution**
with a stability signal per tenant so a drifting variant is visible before it fails.

---

## 5. Escalation & handoff

Run states: `RUNNING → PAUSED_FOR_HUMAN → RESUMING → terminal`. Triggers: the agent
calling `stuck` in discovery; in replay an `UNKNOWN_DIALOG`, an unrecoverable
`SESSION_EXPIRED`, or a risk gate. One path serves both callers.

**The human operates the same live session.** The browser is not restarted and the
run is not re-entered from the top — the page the automation was on is the page the
person acts on. Control is explicit state (`AUTOMATION` / `HUMAN`) with a guard
behind it: the engine asks before it acts, and acting while a human holds control
raises rather than racing. Two writers on one page is how two clicks land on one
confirmation button.

**The console is an interface, not a `print` and an `input`.** Everything that can be
wrong about a handoff is on the resume path, and a handoff only exercisable by a
person at a keyboard gets tested once, by hand, the night before a demo. A scripted
console acts on the live page exactly as an operator would and then answers, which is
why the resume path is the most-tested part of this module.

**Three attention channels, ranked by where the person is actually looking:** a
banner on the live page carrying the decision buttons, a rewritten tab title (what a
minimised window shows), and a terminal block with a bell. Both channels stay live —
the page is occasionally the reason someone was called. The failure being designed
against is not "the operator says no"; it is someone glancing at a finished-looking
browser and closing the window.

**The two pauses resume differently, and that distinction is load-bearing.** A risk
gate pauses *before* the action: the step has not run, so resuming runs it. A dialog
or expired session pauses *after*: the step ran, so resuming re-judges the page and
never re-sends. It is carried in the trace, the schema and both human-facing channels.

**Never blind-resume.** Resuming re-checks the page, not the answer — an operator who
resumes without fixing anything gets the same request back, bounded, then the run
stops. Verified by a real run: the operator clicked resume with the modal still up,
the engine refused, and the record shows `paused_again` then `verified`.

**Wording is mitigation; the checkpoint is the guarantee.** The first human run of
this seam ended in a `HARD_FAILURE` because the interface told the operator to act
when it was asking for permission — and after that was fixed, the next operator
pressed the application's confirm button anyway, which is simply what people do when
a confirm button is on screen. So: if a step the operator approved then fails to
resolve its target, the engine checks that step's own checkpoint, and if the expected
state is already present the action is logged and **not re-sent**. Narrow by
construction — resolution is attempted first, and a step without a checkpoint fails
loudly rather than guessing, because this is the irreversible step and a re-send is a
second sub-account on a member's record. The full story is [FOUND_GAPS.md](FOUND_GAPS.md) G-20.

**What the human did is captured as observed state transitions, not keystrokes** —
before/after URL and DOM around the handoff. Two reasons, the second binding: the
engine is blocked while they work, and instrumenting the page to record typing would
capture credentials into the trace, which §6 makes structurally impossible everywhere
else.

The final result carries an `intervention_record` on **every** terminal status: who
took over, at which step, what the page did, when control returned, and whether the
checkpoint held. It is a list rather than one entry, because a capability with a
mutating step and an irreversible one pauses twice — and the entry that would have
been dropped is a human approving a write to a member's account.

**Mocked deliberately:** the operator console is a terminal prompt and banner buttons,
not a co-browsing UI. The mechanism is real — same session, explicit control
transfer, capture, re-verification, audit record.

---

## 6. Safety

**Allowlist** (`policy.yaml`): permitted origins, routes and action types, enforced
in the executor *before* the action reaches the browser, in both modes. A rejected
action never happens rather than being audited afterwards.

**Risk is classified from what the browser actually did** — the method of the
submitted form and the route it posts to — never from the model and never from a
button's wording, because "Confirm" means nothing on its own. A GET is read-only by
construction. A POST is a mutation unless its route says otherwise (signing in POSTs
and creates nothing). An unrecognised POST is `mutating`: over-classifying costs an
approval flag, under-classifying costs an unreviewed write. Crucially only a control
that *submits* counts — every field on a POST form belongs to that form, so without
that distinction clicking a dropdown reads as a mutation and a capability reports
five mutating steps when it has one.

**Gates:** `read_only` auto-allowed; `mutating` requires `--approve-mutations` or it
pauses; `irreversible` always pauses, standing approval or not — a blanket flag is a
statement about a class of actions, not consent to a particular unrepeatable one.
Conservative on purpose, which is the right default in regulated finance. The tests
assert the gate against the *application's own state*, not the envelope: a gate that
reports without stopping is the failure mode worth pinning.

**Redaction is structural rather than filtered.** The model is told to type the
literal token `{secrets.operator_password}`; the executor substitutes the real value
at the keystroke. The secret is therefore *absent* from the transcript, the trace, the
artifact and the model's context — not scrubbed from them. A password field's value is
never read during element distillation, so it does not exist in our process to leak.
Business values are separate: a `read` step records the value's shape and a masked
form in the trace, while the figure itself travels only in the caller-bound envelope.

**Limits, stated plainly:**

- `operator` in the audit record is the local account or `CUA_OPERATOR` — who was *at
  the machine*, not an authenticated identity. Real attribution comes from whatever
  authenticates the operator console, which is the piece deliberately mocked.
- The allowlist is origin-and-route, not content. It cannot tell a legitimate POST
  from a legitimate-looking one; the risk gate is what covers that.
- No rate limiting, and no defence against a hostile *artifact*. Artifacts are
  trusted input, produced by our own distiller and schema-validated. A capability
  catalog accepting third-party artifacts would need more.
- Chaos flags are demo scaffolding in the target app, armed only by an explicit
  `--chaos` flag from the run's own browser session.

---

## 7. Cuts

**Cut deliberately, seam left real:**

- **Operator console UI.** A terminal prompt plus banner buttons. The handoff
  mechanism — same live session, control transfer, capture, re-verification, audit
  record — is real; only the surface a human touches is minimal.
- **Desktop surface.** Designed, not built (§4). One implementation of `Surface`
  exists; a second is where the abstraction would earn or lose its keep.
- **Multi-tenant variants.** The fingerprint/override seam is in the schema; route
  canonicalization and per-variant resolution are not built.
- **Cross-process resume.** The pause record is forensics. Resuming in a second
  process would mean taking over a Playwright connection the first process owns and
  is blocked on — a materially larger problem, and not what makes the handoff real.
  Open in [FOUND_GAPS.md](FOUND_GAPS.md) O-03.
- **Assisted fallback**, **artifact catalog**, **code generation from artifacts** —
  stretch goals, not attempted. Depth over breadth.
- **Target app realism** where it costs determinism: sub-account numbers are a pure
  function of member and account type, so opening two of the same type yields the
  same number. Wrong for a ledger, right for reproducible evidence, and the app is a
  prop evaluated on nothing (O-04).

**What I would build next, in order:**

1. **A structural drift hash.** The current check catches changes someone remembered
   to announce. A tag-skeleton hash of the pages a capability touches catches the
   reordered row that breaks locators and leaves the build string untouched. This is
   the single highest-value addition, because it converts drift from a postmortem
   finding into a pre-flight warning.
2. **Multi-run stability scoring.** Replay N times, report flakiness per capability
   and per strategy tier. That number is what would let unattended replay be gated on
   an approval state (`draft → approved`) rather than on someone's judgement.
3. **The catalog.** Artifacts exposed as typed, callable capabilities an agent
   discovers by name rather than being handed a filename — the schema is already the
   tool definition, so this is mostly plumbing.
4. **A second surface**, to find out what in `distill/` is browser-shaped. I expect
   it to be more than I think.

**On process:** [FOUND_GAPS.md](FOUND_GAPS.md) logs every gap found while building,
classed as coverage, defect, design or environment. Two entries are worth reading as
evidence of how this was built rather than what it does: **G-11**, where fixing one
prompt bug introduced a worse one — an artifact whose success checkpoint was a
member's balance, so it could only ever succeed for the member it recorded — and
**G-20**, where the first human-driven handoff failed because the interface asked for
permission and told the operator to act.
