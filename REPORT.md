# Design write-up

A system that lets an AI agent operate legacy back-office software that offers no API.
An LLM works the task out once against a live surface; the run is compiled into a typed
capability artifact; after that it executes deterministically with no model in the
decision loop.

**Discovery is a compiler. Replay is a VM. The artifact is the bytecode, and its schema
is the instruction set.**

Spec: [DESIGN.md](DESIGN.md), written before the code. Build log: [FOUND_GAPS.md](FOUND_GAPS.md).
Every claim below is a recorded run in [`evidence/`](evidence/README.md).

---

## 1. Architecture

Two modes over one executor; the layout is in [README](README.md).

**The executor is shared by discovery and replay, deliberately.** If the two used
different machinery to click, a capability could pass discovery and fail replay for
reasons unrelated to the application — and the artifact schema, the thing actually under
test, would be charged for it.

**Nothing above `executor/surface.py` imports Playwright.** That file is a small
protocol — observe, navigate, click, type, read — plus two dataclasses. The artifact
records *what was targeted and how it was identified*, never which library clicked. That
is the seam a desktop surface slots into (§4).

**Perception is hybrid: a screenshot plus a distilled element list.** Pure DOM selectors
contradict the no-clean-DOM reality; coordinates alone make replay fragile. Recording
everything about a target at discovery time is what lets the artifact carry a fallback
chain instead of one guess. The list includes text, not only controls — a balance is not
clickable.

**Three config files, because three kinds of knowledge change for different reasons and
are reviewed by different people:** what automation is *permitted* to do, what a business
result *means*, how to *recognise* a condition on this surface. The sharpest line is
between the last two — a business outcome is capability-specific, declared and terminal;
a runtime condition is app-wide and mostly recoverable. Conflating them is how "the page
was slow" reaches a caller dressed as a result.

**Trade-offs.** A hand-rolled agent loop rather than a framework, because the loop
structure is itself under evaluation; one earns its place at multiple concurrent
capabilities or durable multi-agent state. Single process and synchronous Playwright,
because the handoff needs a live session a human can take over and a job queue makes that
materially harder; the cost is no concurrency.

---

## 2. Artifact schema

One JSON file per capability, validated on save *and* on load; shape in
[DESIGN §5](DESIGN.md).

**`target.strategies` is an ordered fallback chain, not a selector.** `label` first,
because on a legacy surface the text beside a field outlives a table reshuffle; then
`structural`, exact today and brittle to layout edits; then `coordinates`, which never
travel without `verify_text_nearby` — never click blind. Replay logs which tier won, so
you can see which one is carrying the weight before it fails.

**`expected_outcomes` are declared in the contract, not discovered at runtime.** That is
what keeps "no such member" from ever being a crash: the engine never has to decide
whether an unfamiliar page is a failure or an answer.

**Per-step `risk` and per-step `checkpoint`**, so the policy gate hangs off every step
and a failure is attributed to the step that caused it rather than surfacing later as an
unresolvable target.

**Extraction lives on the step, not at the end.** Extracting after the walk cannot work —
by the last step the page holding the value is usually gone. `outputs.*.source_step`
back-references the producing step, and the validator enforces that correspondence
because JSON Schema cannot express it.

**`{secrets.*}` and `requires_secrets` make redaction structural.** A capability declares
credentials by name and values resolve from the environment at run time. An artifact that
stored a password would have to store it somewhere, and there is nowhere.

**`app_fingerprint` is scraped from the application, never authored by us** — a
fingerprint we write cannot mismatch, and a check that cannot fail is decoration. Input
names come from on-screen labels (`Member number` → `member_number`) rather than a
curated mapping nobody could keep in step with the UI.

---

## 3. Determinism & error handling

**Zero model calls is enforced by the contract.** The result schema constrains
`mode: "replay"` to `llm_call_count: 0`, and the count is measured. A replay result
carrying a model call is not a warning — it is an invalid document.

**The recorded chain is executed, never regenerated.** Rebuilding locators live would
quietly repair drift instead of reporting it.

**Three classes, and the ordering between them is the design.** After every step, a
declared **business outcome** is terminal and the caller branches on it; a **recoverable
condition** is handled by policy and logged, *never* terminal; a **hard failure** is
terminal with maximum forensics and deliberately no remediation advice — if the engine
knew the fix it would be a recoverable condition, so `additionalProperties: false` makes
adding a suggestion a schema error.

The outcome scan runs **before** a missing checkpoint is treated as a failure. For member
`99999`, step 6 expects "Member Profile" and does not get it; a checkpoint-first engine
calls that a crash. That ordering decision is most of what the three-class rule buys.

**Waiting is executor policy, never model-decided** — the agent has no `wait` tool,
because a model that can wait papers over a broken step instead of declaring itself
stuck. Attempts are bounded at 5s with retries supplying the tolerance, so a slow page is
reported as *late and by how much* rather than absorbed.

**A timed-out action is re-checked, never re-sent.** A submitted form can be processing
while the browser gives up waiting, and re-sending is how automation double-posts. The
rule recurs twice in §5.

Conditions live in `runtime.yaml`, tabulated in [README](README.md). `SESSION_EXPIRED` is
recoverable **only** because a re-login routine is declared, and that routine is ordinary
artifact steps run by this same interpreter — a recovery path with its own machinery
would be a second, less-tested engine running when things are already wrong.

**UI drift** warns rather than fails; the strategy chain is what copes. A declared build
only catches announced changes — a reordered `<tr>` breaks locators and leaves the
version untouched (§7).

---

## 4. Heterogeneity & multi-tenant

**Surface abstraction.** The seam is `Surface` plus `Element`: three verbs and an
observation. A desktop surface driven by OS-level automation would implement the same
protocol and produce the same `Element` records, and neither the agent loop nor a saved
artifact would change. The strategy chain maps cleanly — `label` becomes the
accessibility name, `structural` the control-tree path, `coordinates` stay coordinates
and still refuse to travel without nearby text to verify.

The honest limit: `executor/browser.py` is the only implementation, and a second would
exert real pressure on `distill/`, which reads a browser-shaped trace. Building against
one surface and *claiming* two is the brittle assumption the brief warns about, so it is
listed as designed, not done.

**Multi-tenant reuse** turns on a split already in the schema: `app` is the stable routing
key — which capabilities and recovery routines belong to this application — while
`app_fingerprint` is `app@build` and is *expected* to differ per tenant and per upgrade.
That is the seam variants hang from: a base artifact plus an override layer keyed by
fingerprint, overriding only the steps that differ rather than re-recording per tenant.
Reuse degrades gracefully rather than failing closed, because the strategy chain tolerates
one tier breaking and drift warns instead of refusing.

Two pieces would make it real, neither built: **canonicalization**, so `/member/12345` is
stored as a pattern rather than a literal — the distiller already templates inputs into
checkpoints and URLs — and **per-variant override resolution** with a stability signal
per tenant.

---

## 5. Escalation & handoff

Run states: `RUNNING → PAUSED_FOR_HUMAN → RESUMING → terminal`. Triggers: the agent
calling `stuck` in discovery; in replay an `UNKNOWN_DIALOG`, an unrecoverable
`SESSION_EXPIRED`, or a risk gate. One path serves both callers.

**The human operates the same live session.** The browser is not restarted and the run is
not re-entered from the top. Control is explicit state (`AUTOMATION` / `HUMAN`) with a
guard behind it: acting while a human holds control raises rather than races. Two writers
on one page is how two clicks land on one confirmation button.

The request reaches them on the page itself, not only in the terminal, because that is
where they are looking — the failure designed against is someone closing a
finished-looking window.

**The two pauses resume differently, and that distinction is load-bearing.** A risk gate
pauses *before* the action: the step has not run, so resuming runs it. A dialog or
expired session pauses *after*: the step ran, so resuming re-judges the page and never
re-sends.

**Never blind-resume.** Resuming re-checks the page, not the answer. An operator who
resumes without fixing anything gets the same request back, bounded, then the run stops —
verified by a real run whose record shows `paused_again` then `verified`.

**Wording is mitigation; the checkpoint is the guarantee.** The first human run of this
seam ended in a `HARD_FAILURE` because the interface told the operator to act while
asking for permission; once fixed, the next operator pressed the confirm button anyway.
So an approved step that then cannot resolve its target is checked against its own
checkpoint, and if the expected state is already there the action is logged and **not
re-sent** — a re-send here means a second sub-account on a member's record
([FOUND_GAPS](FOUND_GAPS.md) G-20).

**What the human did is captured as observed state transitions, not keystrokes.**
Instrumenting the page to record typing would capture credentials into the trace.

Every terminal status carries an `intervention_record`: who took over, at which step,
what the page did, whether the checkpoint held. A list, because a capability with both a
mutating and an irreversible step pauses twice, and the dropped entry would be a human
approving a write.

**Mocked deliberately:** the console is a terminal prompt plus banner buttons. The
mechanism is real — same session, explicit control transfer, capture, re-verification,
audit record.

---

## 6. Safety

**Allowlist** (`policy.yaml`): permitted origins, routes and action types, enforced in the
executor *before* the action reaches the browser, in both modes. A rejected action never
happens rather than being audited afterwards.

**Risk is classified from what the browser actually did** — the method of the submitted
form and the route it posted to — never from the model and never from a button's wording,
because "Confirm" means nothing on its own. An unrecognised POST is `mutating`, since
under-classifying costs an unreviewed write. Only a control that *submits* counts, or a
dropdown reads as a mutation and a capability reports five mutating steps when it has one.

**Gates:** `read_only` auto-allowed; `mutating` requires `--approve-mutations`;
`irreversible` always pauses, standing approval or not — a blanket flag is a statement
about a class of actions, not consent to one unrepeatable one. Each gate is tested
against the *application's own state*, not the envelope.

**Redaction is structural rather than filtered.** The model types
`{secrets.operator_password}` and the executor substitutes at the keystroke; a password
field's value is never read during distillation, so it does not exist in our process to
leak.

**Limits, stated plainly:**

- **Substitution protects a secret on the way in, not afterwards.** The operator ID is
  substituted identically but sits in an ordinary textbox, so the next observation reads
  it back into the transcript. The boundary is the control type, enforced structurally —
  not the token, which defers exposure by one turn ([FOUND_GAPS](FOUND_GAPS.md) G-22).
- `operator` in the audit record is the local account — who was *at the machine*, not an
  authenticated identity.
- The allowlist is origin-and-route, not content; the risk gate covers intent. Artifacts
  are trusted input from our own distiller, and a catalog taking third-party ones would
  need more.

---

## 7. Cuts

**Cut deliberately, seam left real:**

- **Operator console UI** — a terminal prompt plus banner buttons.
- **Desktop surface** — designed, not built (§4).
- **Multi-tenant variants** — the fingerprint/override seam is in the schema; route
  canonicalization and per-variant resolution are not.
- **Cross-process resume** — resuming elsewhere means taking over a Playwright connection
  the first process owns and is blocked on, which is not what makes the handoff real
  (O-03).
- **Assisted fallback, artifact catalog, code generation** — stretch goals. Depth over
  breadth.
- **Target app realism where it costs determinism** — sub-account numbers are a pure
  function of member and account type, so two of the same type collide. Wrong for a
  ledger, right for reproducible evidence (O-04).

**What I would build next, in order:**

1. **A structural drift hash**, turning drift from a postmortem finding into a pre-flight
   warning. Smaller than it sounds: the handoff capture already hashes a page's DOM, so
   this is the same mechanism with text stripped and compared against a value stored at
   record time.
2. **Multi-run stability scoring** — replay N times, report flakiness per capability and
   per strategy tier, letting unattended replay be gated on an approval state rather than
   on judgement.
3. **The catalog** — artifacts exposed as typed, callable capabilities an agent discovers
   by name; the schema is already the tool definition.
4. **A second surface**, to find out how much of `distill/` is browser-shaped.

**On process:** [FOUND_GAPS.md](FOUND_GAPS.md) classes every gap found while building as
coverage, defect, design or environment. Two show how: **G-11**, where fixing one prompt
bug introduced a worse one — an artifact whose success checkpoint was a member's balance,
so it could only succeed for the member it recorded — and **G-20**, where the first
human-driven handoff failed because the interface asked for permission and told the
operator to act.
