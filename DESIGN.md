# Computer-Use Automation System — Design Document

This is the build spec. Every decision here was made deliberately before code was written.
It is the single source of truth for Claude Code sessions. Do not deviate from the contracts
defined here without updating this document first.

**The through-line:** The model discovers. The artifact becomes a reusable capability.
Deterministic replay is how an AI agent invokes it in production.

---

## 1. System overview

Two separate pieces of software:

1. **Target app** (`/target_app/`) — a deliberately legacy-style fake credit union operator
   portal. A prop. Gets zero evaluation weight itself; exists so the automation has a
   realistic, controllable surface. Runs locally.
2. **The system** (`/cua/`) — what is actually evaluated. A Python CLI with two modes:
   - **Discovery**: an LLM-driven observe → decide → act loop completes a natural-language
     goal against the target app, producing a step trace. A distiller converts the trace
     into a capability artifact.
   - **Replay**: an interpreter executes a saved artifact with new input parameters.
     Zero LLM calls. This is the production path.

No frontend. The operator console for human handoff is a terminal prompt (mocked
deliberately; the handoff *mechanism* is real).

Mental model: discovery is a compiler (expensive, LLM-driven, run once);
replay is a VM (cheap, deterministic, run forever). The artifact is the bytecode,
and its schema is the instruction set.

### Stack (decided)

- Python 3.11+, Playwright (Chromium, headed for demos)
- Anthropic API (Claude) for discovery only; vision + tool use
- Hand-rolled agent loop (~150 lines). Deliberate non-choice of LangGraph: single linear
  agent, and the loop structure itself is under evaluation. (Production threshold where a
  framework would earn its place: multiple concurrent capabilities, durable multi-agent state.)
- Artifacts: JSON, validated by JSON Schema on save and on load
- Target app: small Python server (Flask or stdlib), server-rendered HTML

### Repo layout

```
/README.md            setup + exact demo commands
/REPORT.md            design write-up (their 7 headings, their order)
/DESIGN.md            this file
/target_app/          the fake credit union portal
/cua/                 the system (named for the CLI: `python -m cua`, §12)
  executor/           surface abstraction, element distillation, waiting, actions
  agent/              discovery loop, tool definitions, prompts
  distill/            trace → artifact
  replay/             artifact interpreter, wait/retry policy, outcome scanner
  policy/             allowlist, risk gates, redaction
  hitl/               pause/resume state machine, terminal operator prompt
  contracts/          JSON Schemas: artifact, result envelope
  config.py           model, stopping conditions, {secrets.*} resolution
  evidence.py         per-run directory, trace/transcript writers
/policy.yaml          allowlist + per-step risk routes (§8)
/outcomes.yaml        business outcomes declared per app (§5, §6)
/runtime.yaml         runtime-condition recognisers + recovery routines (§6)
/capabilities/        saved artifacts (one JSON per capability)
/evidence/            per-run directories (see §10)
```

---

## 2. Target app spec (build first, half-day time box)

A fake credit union operator portal, styled like 2003 enterprise software.

**Pages (server-rendered, hostile markup):**
1. `/login` — operator login (any credentials accepted; session cookie set).
   Logged-in operator is hardcoded: name "E. Okafor", region **Eastern**.
2. `/search` — member lookup by 5-digit member number.
3. `/member/<id>` — member profile: name, region, savings balance, loan status,
   loan amount, credit score. (Data model: id, name, age, region, savings_balance,
   loan_taken, loan_amount, credit_score.)
4. `/member/<id>/sub-account/new` — multi-field form → confirmation screen →
   "Sub-account opened" result page. (The mutation flow.)

**Hostile-markup rules (this is the point of building it ourselves):**
- Layout via nested `<table>` elements; no CSS frameworks
- No `id` attributes, no `data-*` test hooks, no semantic HTML5 elements
- Generic class names (`.c1`, `.row2`) reused for different things
- Form fields identified only by adjacent `<td>` label text
- At least one frame or iframe on the member profile page

**Deterministic behavior rules (all keyed off member ID, so evidence is reproducible):**
- IDs `10000–49999` → Eastern region (accessible to the operator)
- IDs `50000–89999` → Western region → profile access renders a
  "Member outside your region" denial screen (PERMISSION_DENIED trigger)
- ID `99999` → "No member matches this number" (MEMBER_NOT_FOUND trigger)
- Seed ~10 members with plausible fake data. No real PII, ever.
- **Opened sub-accounts persist in process memory** and appear on the member's
  profile; a deposit funded from primary savings reduces the savings figure, one
  funded at the branch does not. The irreversible step has to leave a mark a later
  page can see, or the risk gate is guarding an illusion. Memory rather than a
  database so seeded data stays deterministic: a demo starts from a known state by
  starting the server.

**Self-reported build (feeds `app_fingerprint`, §5):** every page carries the app's
own identity — `<meta name="generator" content="legacy-cu-portal@4.3.0">` plus a
visible `build 4.3.0` in the masthead. Not a test hook: it identifies the
*application*, not any element, so it gives the automation no help locating
anything. The build is overridable via the `TARGET_APP_BUILD` environment variable
so drift can be demonstrated without editing code (record at the shipped build,
replay against a bumped one, watch the warning fire). The build is raised by hand
whenever the surface changes — it went to 4.3.0 when the profile page grew its
sub-accounts table — because a version that never moves is a version that can
never disagree with a recording.

**Chaos flags (query params, for injecting runtime conditions during replay demos):**
- `?chaos=slow` — 8s delay on next page load (recoverable: wait/retry)
- `?chaos=session` — expire session; next action bounces to `/login`
  (recoverable if re-login is scripted; otherwise escalates)
- `?chaos=dialog` — inject an unexpected modal ("Scheduled maintenance at 6 PM")
  on next page (unknown dialog → NEEDS_INTERVENTION)
- `?chaos=error` — next action returns an HTTP 500 page (HARD_FAILURE)

---

## 3. Perception & action mechanism (hybrid — decided)

Per loop turn, the agent receives:
1. A screenshot of the current viewport
2. A numbered list of elements, distilled by the executor from the live page:
   index, role, label/nearby text, structural path, bounding box

   **Not only interactive ones.** A savings balance is not clickable, so a list of
   controls alone makes the `read` tool unusable and pushes extraction into OCR of
   the screenshot. Text-bearing cells are elements too: a label/value pair in a
   table row collapses into a single element that *is* the value and carries its
   neighbour's text as the label — which is what makes `label: "Savings balance"` a
   read target. The executor lives in `/cua/executor/` rather than under `agent/` or
   `replay/` because both use it; if they used different machinery, a capability
   could pass discovery and fail replay for reasons unrelated to the app.

The agent responds with exactly one tool call. The executor performs it via Playwright.

Rationale: pure DOM selectors contradict the "no clean DOM" reality; pure
screenshot+coordinates makes replay fragile. The hybrid records *everything* about each
target at discovery time, so the artifact can carry an ordered fallback chain per target:

1. `label` — role + visible/adjacent label text (most stable on legacy surfaces)
2. `structural` — CSS/XPath-ish structural path (brittle to layout edits, exact today)
3. `coordinates` — bounding-box center **plus** `verify_text_nearby` (never click blind)

Replay tries strategies in order and logs which one won (drift telemetry for free).

---

## 4. Agent action space (6 tools — the loop contract)

Every tool call is appended to the step trace with its full context. The artifact is
distilled from this trace, so what tools record determines what artifacts can express.

| Tool | Params | Executor behavior | Records into trace |
|---|---|---|---|
| `click` | `element_index`, `reason` | Click via Playwright | Full element descriptor (label, role, structural path, bbox) — not just the index |
| `type` | `element_index`, `text`, `reason` | Focus + fill | Descriptor + literal text; distiller templates goal-derived values → `{inputs.*}` |
| `navigate` | `url`, `reason` | **Allowlist check first**, then goto | URL (canonicalized) |
| `read` | `element_index`, `label`, `reason` | Extract text content | Label, raw value, descriptor → becomes `outputs` + extraction targets |
| `done` | `summary`, `outputs`, `success_evidence` | End loop, success | `success_evidence` becomes the artifact's success checkpoint |
| `stuck` | `reason`, `blocker_description`, `requested_action` | Raise intervention request | Feeds NEEDS_INTERVENTION payload verbatim |

**Deliberate absences:** no `hover` (statefulness for near-zero value here); no `wait`
(waiting is executor policy — a model that can choose to wait will paper over problems
instead of declaring them). Stopping conditions (max 25 steps, 5-min wall clock) are
enforced by the loop, never chosen by the model.

**Discovery logging:** every LLM request/response is logged; `llm_call_count` is tracked
per run and asserted `== 0` on every replay.

---

## 5. Artifact schema (the capability contract)

One JSON file per capability in `/capabilities/`. Validated against
`/src/contracts/artifact.schema.json` on save and load. Reference shape:

```json
{
  "schema_version": "1.0",
  "capability": {
    "name": "lookup_member_balance",
    "version": "1.0.0",
    "description": "Log into the member portal, search a member by ID, return their savings balance.",
    "requires_secrets": ["operator_id", "operator_password"],
    "recorded_against": {
      "app": "legacy-cu-portal",
      "app_fingerprint": "legacy-cu-portal@4.3.0",
      "recorded_at": "<iso8601>",
      "discovery_run_id": "run_..."
    }
  },
  "inputs": {
    "member_number": { "type": "string", "pattern": "^[0-9]{5}$", "required": true,
                   "description": "5-digit member number" }
  },
  "outputs": {
    "member_name":    { "type": "string", "source_step": 4 },
    "savings_balance": { "type": "number", "source_step": 5 }
  },
  "expected_outcomes": [
    { "code": "MEMBER_NOT_FOUND",
      "detect": { "condition": "text_present", "value": "No member matches" },
      "meaning": "Legitimate result: no such member. Not a failure." },
    { "code": "PERMISSION_DENIED",
      "detect": { "condition": "text_present", "value": "outside your region" },
      "meaning": "Operator's region scope excludes this member. Caller decides next move." }
  ],
  "steps": [
    { "id": 1, "action": "navigate", "url": "{base_url}/login", "risk": "read_only",
      "checkpoint": { "condition": "text_present", "value": "Operator Login" } },
    { "id": 2, "action": "type",
      "target": { "strategies": [
        { "kind": "label", "value": "Member number" },
        { "kind": "structural", "value": "form table tr:nth-of-type(1) input" },
        { "kind": "coordinates", "value": [412, 288], "verify_text_nearby": "Member number" } ] },
      "value": "{inputs.member_number}", "risk": "read_only" },
    { "id": 5, "action": "read",
      "target": { "strategies": [ { "kind": "label", "value": "Savings balance" } ] },
      "output": "savings_balance", "risk": "read_only" }
  ],
  "success": {
    "checkpoint": { "condition": "text_present", "value": "Member Profile" }
  }
}
```

**Load-bearing choices (defend these in REPORT.md):**
- `target.strategies` — ordered fallback chain; the locator-robustness story
- `expected_outcomes` — business outcomes are *declared in the contract*, not discovered
  at runtime; this is what keeps "no such member" from ever being a crash
- Per-step `risk` (`read_only` | `mutating` | `irreversible`) — the policy gate hangs off
  every step
- Per-step `checkpoint` — "the click worked" is verified, never assumed
- `app_fingerprint` — drift detection hook; on mismatch, replay warns (and this field is
  the seam for per-tenant variants later). `app` is the stable routing key (which
  capabilities belong to this application, which recovery routines apply); the
  fingerprint is `app@build` and is *expected* to change. The value is scraped from
  the app itself, never hardcoded by the system — a fingerprint we author cannot
  mismatch, and a check that cannot fail is decoration.
- `{inputs.*}` templating — the moment a recording becomes a parameterized capability
- `{secrets.*}` templating + `requires_secrets` — a capability declares which
  credentials it needs by *name*; values are resolved from the environment at run
  time and never enter the file. §8's redaction rule made structural: an artifact
  that stored a password would have to store it somewhere, and there is nowhere.
- **Extraction lives on the step, not at the end.** A `read` step names the output
  it fills. The alternative — extracting from a `success.extract` block after the
  walk — cannot work: by the last step the page holding the value is usually gone.
  `outputs.*.source_step` back-references the producing step, and the validator
  enforces that correspondence (JSON Schema cannot express it).

**Versioning:** `schema_version` = format; `capability.version` = semver of the recording
(patch: re-record same flow; minor: new optional outputs; major: inputs/outputs change).

---

## 6. Outcome detection & error taxonomy (replay engine behavior)

After **every** step, the replay engine scans two lists:

1. **Capability-specific outcomes** — the artifact's `expected_outcomes`. Match → stop,
   return `BUSINESS_OUTCOME`. (Checked every step because runtime surprises don't respect
   the step where you expect them; detection markers are specific text, so false-positive
   risk is low. Cost: milliseconds.)
2. **Global runtime conditions** — engine config in `runtime.yaml`, shared across
   all capabilities. Three config files because there are three kinds of knowledge,
   changed for different reasons and reviewed by different people: `policy.yaml` is
   what automation is *permitted* to do, `outcomes.yaml` is what a business result
   *means*, `runtime.yaml` is how to *recognise* a condition on this surface. The
   sharpest line is between the last two — a business outcome is capability-specific,
   declared in the artifact, and terminal; a runtime condition is app-wide and mostly
   recoverable. Conflating them is how "the page was slow" reaches a caller dressed
   as a business result.
   - `SLOW_LOAD` → recoverable: retry with backoff, max 3 attempts, then HARD_FAILURE
   - `SESSION_EXPIRED` → recoverable **only** if a `relogin` recovery routine is defined
     for the app; else NEEDS_INTERVENTION
   - `KNOWN_INTERSTITIAL` (declared dismissable dialogs) → recoverable: dismiss, log, continue
   - `UNKNOWN_DIALOG` → NEEDS_INTERVENTION (never guess at a dialog we didn't expect)
   - `APP_ERROR` (HTTP 5xx / error page markers) → HARD_FAILURE

**The three-class rule (the design's spine):**
- Expected business outcome → terminal status the caller branches on
- Recoverable condition → handled by policy, **logged in the step trace, never a terminal
  status** (`"recovered": {"condition": "SLOW_LOAD", "action": "retried", "attempts": 2}`)
- Hard failure → terminal status with maximum forensics

Waiting policy lives in the executor: every step has an implicit readiness wait
(element resolvable + page settled), bounded per step; never model-decided.

---

## 7. Result contract (what every run returns)

Common envelope, status-specific payload. Machine-readable throughout — the caller is an
AI agent, not a human reading prose.

```json
{
  "run_id": "run_20260814_093012",
  "capability": "lookup_member_balance",
  "capability_version": "1.0.0",
  "mode": "discovery | replay",
  "status": "SUCCESS | BUSINESS_OUTCOME | NEEDS_INTERVENTION | HARD_FAILURE",
  "inputs": { "member_number": "12345" },
  "started_at": "...", "ended_at": "...",
  "steps_completed": 6, "steps_total": 6,
  "llm_call_count": 0,
  "evidence_path": "evidence/run_20260814_093012/",
  "payload": {}
}
```

`mode` is load-bearing, not bookkeeping: the schema constrains `mode: "replay"` to
`llm_call_count: 0`, so the system's central claim is enforced by the contract
rather than asserted in prose. A replay result carrying a model call is not a
warning — it is an invalid document.

**SUCCESS** — typed outputs exactly as declared; no prose:
```json
{ "outputs": { "member_name": "Alice Torres", "savings_balance": 4523.18 },
  "checkpoint_verified": true }
```

**BUSINESS_OUTCOME** — a first-class answer the caller branches on:
```json
{ "outcome_code": "MEMBER_NOT_FOUND", "detected_at_step": 3,
  "detail": "Search for member 99999 returned 'No member matches this number'",
  "evidence": "evidence/run_.../step3_outcome.png" }
```

**NEEDS_INTERVENTION** — a live pause, forward-looking (someone must act now):
```json
{ "reason": "UNEXPECTED_DIALOG",
  "detail": "Modal after step 4: 'Scheduled maintenance at 6 PM'",
  "paused_at_step": 4,
  "screenshot": "evidence/run_.../intervention_step4.png",
  "session_id": "browser_sess_7f2a",
  "requested_action": "Dismiss or defer; resume when Member Profile visible",
  "control": "HUMAN" }
```
When the run later completes, its final result carries an `intervention_record`:
who took over, at which step, captured human actions, control-return time, post-resume
checkpoint result.

**Amended during 4b — the record is a list, not a single entry.** This was written
assuming one handoff per run, which the demo has and `open_sub_account` does not:
that capability has a `mutating` step and an `irreversible` one, so an unattended
invocation pauses twice, and a run may also pause repeatedly on the same step when
an operator resumes without having changed anything. Reporting one of those and
discarding the rest loses exactly the entries an audit exists to keep — a human
approving a write to a member's account. So the record carries a per-run
`operator` and an `interventions` array:

```json
"intervention_record": {
  "operator": "e.okafor",
  "interventions": [
    { "paused_at_step": 11, "reason": "MUTATION_NOT_APPROVED", "when": "before_step",
      "requested_action": "Re-invoke with --approve-mutations, or approve this step.",
      "paused_at": "...", "control_returned_at": "...",
      "decision": "resume", "resolution": "verified",
      "human_actions": [ { "recorded_at": "...", "url": "...",
                           "summary": "no observable change" } ],
      "post_resume_checkpoint": { "condition": "text_present",
                                  "value": "Confirm Sub-Account", "passed": true } }
  ]
}
```

Two fields carry the weight. `when` is `before_step` for a risk gate — the step had
not run, so resuming means running it — and `after_step` for a dialog or an expired
session, where the step *did* run and resuming means re-judging the page it landed
on. `resolution` is how the handoff ended (`verified`, `resumed`, `paused_again`,
`not_resumed`, or the terminal kind that followed), which is what makes
"never blind-resume" auditable rather than merely implemented: an operator who
answered without acting leaves a `paused_again` behind. `verified` means a
checkpoint was re-checked and held; `resumed` means control came back with no
checkpoint to re-check, which is every discovery handoff, since a flow still
being discovered has none yet. Separate values so the record never claims a
verification that did not happen.

**`HUMAN_PERFORMED_STEP` — a recovered condition that belongs to the handoff, not
to the app.** §6 enumerates the app-level runtime conditions; this one is added
here because it can only arise while a human holds control. When a step the
operator *approved* then fails to resolve its target, the engine checks that
step's own checkpoint: if the state the recording expected is already present,
the person did it themselves, and the action is logged and **not re-sent**. Same
rule as a timed-out action, reached from the other direction — re-check, never
re-send. It is deliberately narrow: resolution is attempted first, so a control
still on the page is simply clicked, and a step with no checkpoint fails loudly
rather than guessing, because "already done" and "never happened" are otherwise
the same observation and this is the `irreversible` step.

Not a hypothetical. The first human run of this seam ended exactly this way: the
operator was asked to confirm, was standing at a browser showing a confirm
button, and pressed it. The instruction now differs per pause kind and the banner
carries the buttons, but wording is mitigation and the checkpoint rule is the
guarantee.

**Human actions are captured as observed state transitions, not as keystrokes.**
One entry per handoff — the page before control transferred against the page after
it returned — because the engine is blocked on the operator while they work, and
because instrumenting the page to record what they typed would capture credentials
into the trace, which §8 makes structurally impossible everywhere else. The full
before/after DOM snapshots go to evidence; the envelope carries the summary.

**HARD_FAILURE** — maximum debuggability; **no remediation suggestions** (if the engine
knew the fix, it would be a recoverable condition):
```json
{ "failed_at_step": 5, "action_attempted": "click",
  "expected": "checkpoint 'Member Profile' visible within 10s",
  "observed": "page title 'HTTP 500 — Internal Server Error'",
  "strategies_tried": ["label: View profile", "structural: ...", "coordinates+verify"],
  "screenshot": "evidence/run_.../failure_step5.png",
  "dom_snapshot": "evidence/run_.../failure_step5.html" }
```

---

## 8. Safety & policy guardrails

- **Allowlist** (`policy.yaml`): permitted origins/routes + permitted action types.
  Enforced in the executor before any navigate/click/type — in both discovery and replay.
  The agent cannot act outside it; violations end the run.
- **Risk classification** (`policy.yaml`, `risk:`): decided from what the browser
  actually did — the method of the submitted form and the route it posted to —
  never from the model and never from a button's wording, since "Confirm" means
  nothing on its own. A GET is read-only by construction. A POST is a mutation
  unless its route says otherwise (signing in POSTs and creates nothing). An
  unrecognised POST is `mutating`: over-classifying costs an approval flag,
  under-classifying costs an unreviewed write to a member's account. Crucially,
  only a control that *submits* counts — every field on a POST form belongs to
  that form, so without that distinction clicking a dropdown reads as a mutation
  and a capability reports five mutating steps when it has one.
- **Risk gates:** `read_only` → auto-allowed. `mutating` → allowed in replay only if the
  invocation passes `--approve-mutations` (else pause). `irreversible` (e.g., final
  confirmation of sub-account creation) → always pauses for explicit human confirmation
  via the HITL path, discovery and replay alike. Conservative by design; justified in
  REPORT.md as the right default for regulated finance.
- **Redaction:** credentials are handled *structurally* rather than filtered. The
  model is told to type the literal token `{secrets.operator_password}`; the
  executor substitutes the real value at the keystroke, and the trace records the
  token. The secret is therefore absent from the transcript, the trace, the
  artifact and the model's context — not scrubbed from them. A password field's
  value is never even read during element distillation, so it does not exist in
  our process to leak. Business values are separate: a `read` step records the
  value's *shape* and a masked form (`$•••••`) in the trace, which is enough for
  the distiller to type the output, while the figure itself travels only in the
  caller-bound result envelope.

  A redaction layer sits between raw observations and anything persisted.
  Credentials/session tokens never enter traces, artifacts, or logs. Extracted outputs
  are persisted only in the result envelope (caller-bound), not duplicated into debug
  logs. Screenshots on sensitive pages mask value regions where feasible; balances in
  step-trace logs are masked (`$•••••`), full values live only in outputs.
  No real PII anywhere in the repo — all member data is fabricated.

---

## 9. HITL escalation & handoff (state machine + seam)

Run states: `RUNNING → PAUSED_FOR_HUMAN → RESUMING → (terminal)`.

**Triggers into `PAUSED_FOR_HUMAN`:**
- Discovery: agent calls `stuck`
- Replay: `UNKNOWN_DIALOG`, unrecoverable `SESSION_EXPIRED`, or an `irreversible` step
  without standing approval

**The seam (the part that must be real):**
1. On pause: persist run state (artifact position, inputs, trace so far), write the
   intervention request (NEEDS_INTERVENTION payload), keep the **same Playwright
   browser session alive** — the human operates that session, never a fresh one.
2. Control flag flips to `HUMAN`. Automation is inert while a human holds control
   (single writer at a time, tracked explicitly).
3. Operator console = terminal prompt (deliberately mocked): prints the request,
   waits. The human acts directly in the live headed browser window.
4. **Capture what the human did:** while control=HUMAN, record DOM/URL state transitions
   (before/after snapshots at minimum) into the trace as `human_action` entries.
5. On operator "resume": control flips back, engine re-verifies the current step's
   checkpoint (or the pre-pause expected state) before continuing — never blind-resume.
   If verification fails → back to PAUSED_FOR_HUMAN with a new request.
6. Final result includes the `intervention_record`.

**TODO — decide before building 4a: how far does "persist run state" go?**

Step 1 says the pause persists artifact position, inputs and trace. That leaves a
question this document has not answered: is the pause record *forensics*, or is it
a **resume token another process could pick up**?

The two readings cost very different amounts of Day 4:

- **Forensics (same-process resume).** The pause record is written so the pause is
  inspectable and auditable; the run itself resumes in the process that paused,
  driving the browser it already owns. Cheap, and consistent with the rest of §9.
- **Cross-process resume.** A second process reads the record and continues the
  run. But §9's central requirement is that the human operates the *same live
  browser session* — so a second process would have to re-attach to a browser the
  first process owns and is blocking on. That is a materially larger problem
  (session handover, ownership of the Playwright connection, what happens when the
  original process dies) and it is not what makes the handoff *real*; the same
  live session is.

Leaning to forensics/same-process, with the limitation stated in REPORT rather than
left looking like an oversight — an operator console that outlives the run is a
production concern, and the seam is what is being demonstrated. **Not yet decided.**
Whichever way it goes, it changes what the pause record must contain, so it is
settled before the state machine is written, not after.

---

## 10. Evidence & observability

Per run: `/evidence/run_<id>/` containing:
- `trace.jsonl` — structured step log: action, target descriptor, strategy used
  (replay), checkpoint result, outcome scans, recoveries, timings, llm_call_count.
  Page-transitioning steps also record the state they landed in (`after`: URL, and
  a page marker when the document offers text worth asserting). Without it the
  distiller has nothing from which to synthesise a per-step checkpoint, and the
  schema requires one on every navigate and click. Absent a usable marker the
  checkpoint falls back to the URL rather than inventing text — templated, so a
  checkpoint on member 12345's profile still holds for member 23456.
- `screenshots/` — on failure, on outcome detection, on intervention (always);
  per-step optional via `--screenshots=all`
- Discovery runs additionally: full model transcript (requests/responses)
- Failures additionally: DOM snapshot at point of failure

**Demo evidence set (the storytelling minimum, committed to the repo):**
1. Discovery run: goal → completed by LLM (transcript + screenshots)
2. Replay, member `23456` (different from recorded!) → SUCCESS, `llm_call_count: 0`
3. Replay, member `99999` → BUSINESS_OUTCOME `MEMBER_NOT_FOUND`
4. Replay, member `67890` → BUSINESS_OUTCOME `PERMISSION_DENIED` (region rule)
5. Replay with `?chaos=dialog` → NEEDS_INTERVENTION → human handoff → resume → SUCCESS
   with `intervention_record`

---

## 11. Capabilities to record (two)

1. `lookup_member_balance` — read-only: login → search → profile → extract name+balance
2. `open_sub_account` — mutating: profile → form → confirmation screen (final submit is
   `irreversible`, so it exercises the risk gate + HITL confirmation)

---

## 12. CLI surface

```
python -m cua target_app serve                     # start the fake portal
python -m cua discover --goal "look up member 12345 and read their savings balance" \
       --target http://localhost:5000 --save-as lookup_member_balance
python -m cua replay lookup_member_balance --param member_number=23456
python -m cua replay lookup_member_balance --param member_number=99999  # business outcome
python -m cua replay open_sub_account --param member_number=23456 \
       --param account_type="Holiday Club" --param account_nickname="Vacation fund" \
       --param initial_deposit=150.00 --approve-mutations
```

Input names are **derived from the field's on-screen label**, not authored — the
distiller slugifies whatever text names the control (`Member number` →
`member_number`). A hand-picked mapping would read better and would be one more
thing to keep in step with a UI nobody controls.

`replay` also takes `--target`, `--headless`, `--screenshots {failure,all}`, and
`--chaos {slow,session,dialog,error}`. The chaos flag belongs on the invocation
rather than being armed out of band because the target app's flags are
session-scoped: the browser that will experience the condition has to be the one
that arms it, and it arms it mid-flow — armed on the opening navigation, every
condition lands on the login page and proves nothing.

Built so far: `target_app serve`, `validate`, `discover`, `replay`. Day 4 (HITL)
adds no new subcommand: an intervention is a state a run enters, not a command
someone invokes, so it surfaces on `replay`.

---

## 13. Build order (4-day box)

- **Day 1:** this design (done) + target app (half-day, time-boxed) + contracts as JSON
  Schemas + repo skeleton
- **Day 2:** executor (element distillation, screenshots, allowlist) + discovery loop +
  trace logging → first real LLM-driven run; distiller → first artifact
- **Day 3:** replay engine (interpreter, fallback chain, checkpoints, outcome scanner,
  wait/retry, risk gates) + result envelope + chaos-flag demos
- **Day 4:** HITL state machine + terminal operator prompt + intervention capture;
  evidence set; README; REPORT.md (allocate real hours — it is half the submission)

**Cuts (deliberate, for REPORT.md §7):** operator console UI (terminal mock; seam is
real), desktop surface (design only), multi-tenant variants (design only:
fingerprint + override layers), assisted fallback (stretch — only if Day 3 ends clean),
artifact catalog endpoint (stretch), multi-run stability scoring (next step).
