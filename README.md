# Computer-Use Automation System

An LLM discovers how to complete a task in a legacy web app once, and the run is
distilled into a reusable **capability artifact**. After that the task is executed by
a deterministic interpreter with **zero LLM calls** — new inputs, same flow.

Discovery is a compiler. Replay is a VM. The artifact is the bytecode.

**▶ [3-minute demo](https://drive.google.com/file/d/1eIgNsWc4lbPqOccaTLGtm96-e7MSX5na/view?usp=sharing)** — a deterministic replay against a member it never saw, a business outcome, and a human taking over a live run to approve an irreversible step.

See [DESIGN.md](DESIGN.md) for the full spec — it is the source of truth, and this
README only tells you how to run things.

---

## Quick start

Everything below runs offline against a local app. **No API key is needed** to see
the system work — two capabilities are already recorded and committed, and replay
makes no model calls.

```bash
git clone https://github.com/ShahaDeven/Project_interface.git && cd Project_interface

python -m venv .venv
.venv\Scripts\activate            # Windows
source .venv/bin/activate         # macOS / Linux

pip install -r requirements.txt
playwright install chromium       # separate ~140MB download; nothing runs without it

cp .env.example .env              # Windows: copy .env.example .env
```

`.env.example` works **as it is** for replay — the operator credentials in it are
fabricated and the target app accepts anything. Only discovery needs a real
`ANTHROPIC_API_KEY`.

Now two terminals. **Leave the first one running:**

```bash
# terminal 1 — the fake credit union portal, on http://127.0.0.1:5000
python -m target_app
```

```bash
# terminal 2 — replay a committed capability against a member it never saw
python -m cua replay lookup_member_balance --param member_number=23456
```

```
Status: SUCCESS  (7/7 steps, llm_call_count=0)
Outputs:
  savings_balance = 18240.55  (float)
```

That is the whole claim in one command: recorded against member `12345`, run
against `23456`, zero model calls. Then the same artifact with an input that has no
answer:

```bash
python -m cua replay lookup_member_balance --param member_number=99999
#  BUSINESS_OUTCOME  MEMBER_NOT_FOUND — an answer the caller branches on, exit 0
```

### Recording one yourself

This is the only step that calls a model and costs money. Put a real key in `.env`
first; skip it entirely if you would rather not — the two committed capabilities
were produced by exactly this command, and their full model transcripts are in
[`evidence/`](evidence/README.md).

```bash
python -m cua discover --goal "look up member 12345 and read their savings balance" --target http://127.0.0.1:5000 --save-as lookup_member_balance
```

**→ [`evidence/`](evidence/README.md) — thirteen recorded runs with an index.** Every
claim in this README is a run you can open: discovery, deterministic replay, both
business outcomes, three recovered runtime conditions, a hard failure, both risk
gates, and three handoffs. Start there if you would rather read than run.

---

347 tests cover the system, and two capabilities were recorded from real LLM-driven
runs. Gaps found while building — including the two that only surfaced when a human
drove the handoff — are logged in [FOUND_GAPS.md](FOUND_GAPS.md).

---

## Setup, in more detail

**Python 3.11+.** Everything else is in `requirements.txt`, except the browser.

**Why `playwright install chromium` is a separate command.** `pip install
playwright` fetches only the Python client. The Chromium binary is a ~140MB
download that lands in a user cache outside `site-packages`, so it cannot go in
`requirements.txt` — pip resolves packages, and a browser is not one. Skip it and
nothing breaks confusingly: browser tests are *skipped* with that command in the
message, the other 269 still run, and the CLI exits 2 saying the same thing rather
than raising a Playwright stack trace.

**What `.env` holds.** Copy `.env.example` to `.env` (gitignored); the values in it
already work.

```
ANTHROPIC_API_KEY=sk-ant-...          # discovery only — replay never reads it
CUA_SECRET_OPERATOR_ID=e.okafor
CUA_SECRET_OPERATOR_PASSWORD=not-a-real-password
```

The two `CUA_SECRET_*` values are the operator credentials a capability declares in
`requires_secrets`, and replay does need them — a capability that lists a secret
refuses to start without it, in milliseconds, before a browser opens. The model is
never handed either one: it types the token `{secrets.operator_id}` and the
executor substitutes the value at the keystroke.

**What that protects, precisely.** The **password never enters a transcript, trace
or artifact**, because a password field's value is never read during element
distillation — it does not exist in our process to leak. The **operator ID is
substituted the same way but is read back like any other text input**, so once
typed it does appear in the discovery transcript's observations of the page. That
is a non-secret identifier and the exposure is accepted, but the boundary is worth
stating plainly: a value that must not appear on screen belongs in a
password-typed control, which is the one thing the executor enforces structurally.
Substitution protects a secret at the keystroke, not afterwards — see
[FOUND_GAPS.md](FOUND_GAPS.md) G-22.

The target app accepts anything; these exist so the mechanism is real rather than
mocked.

---

## Run the target app

```bash
python -m target_app          # or: python -m cua target_app serve
```

Serves **http://127.0.0.1:5000**. Ctrl+C to stop. Every command below needs it
running.

It is a deliberately hostile 2003-era credit union operator portal: nested-table
layout, no `id` attributes, no `data-*` hooks, no semantic HTML5, generic class
names reused for unrelated things, form fields identified only by adjacent `<td>`
text, and loan details buried in an unnamed `<iframe>`. That is the point — it is
the surface the automation has to cope with. It is a prop, and it is evaluated on
nothing.

**Sign in with any username and password.** The operator is hardcoded as
*E. Okafor*, region **Eastern**.

### Members worth trying

Behaviour is a pure function of the member number, so evidence runs reproduce
exactly.

| Number | What happens | Why it exists |
|---|---|---|
| `12345` | Alice Torres, `$4,523.18` | the flow gets recorded against this one |
| `23456` | Marcus Bell, `$18,240.55` | replayed with — deliberately *not* the recorded one |
| `67890` | "Member outside your region" | Western member → `PERMISSION_DENIED` |
| `99999` | "No member matches this number" | → `MEMBER_NOT_FOUND` |

Ten members are seeded. `10000–49999` are Eastern (reachable), `50000–89999` are
Western (denied). All member data is fabricated — there is no real PII anywhere in
this repo.

---

## Record a capability

With the target app running, this is a genuine LLM-driven run — the model has never
seen this app and is told nothing about it:

```bash
python -m cua discover --goal "look up member 12345 and read their savings balance" --target http://127.0.0.1:5000 --save-as lookup_member_balance
```

It opens a visible browser (add `--headless` to suppress), works out that the portal
needs signing into, finds the search field by the text beside it, reads the balance,
and writes `capabilities/lookup_member_balance.json`. Everything it did lands in
`evidence/run_<timestamp>/` — `trace.jsonl`, the full model `transcript.jsonl`,
per-step screenshots, and a `result.json`.

Two capabilities are committed, both recorded this way:

| Capability | Steps | Inputs | Risk |
|---|---|---|---|
| `lookup_member_balance` | 7 | `member_number` | all `read_only` |
| `open_sub_account` | 13 | 4 | 1 `mutating`, 1 `irreversible` |

Nothing about them is hand-authored. Input names come from the on-screen labels,
per-step risk from `policy.yaml`, and business outcomes from `outcomes.yaml` scoped
to the routes each flow visits.

---

## Replay a capability

The production path. Same artifact, new inputs, **zero model calls** — no API key
is involved, and `llm_call_count` is counted rather than declared.

```bash
python -m cua replay lookup_member_balance --param member_number=23456
```

Recorded against member `12345`, run against `23456`, and it ends with a typed
number rather than a sentence about one:

```
Status: SUCCESS  (7/7 steps, llm_call_count=0)
Outputs:
  savings_balance = 18240.55  (float)
Evidence: evidence/run_20260814_183900/
```

The same artifact is meant to produce a different ending per input, and the
ending is the point:

| Command | Ends with | Exit |
|---|---|---|
| `--param member_number=23456` | `SUCCESS`, `savings_balance = 18240.55` | 0 |
| `--param member_number=99999` | `BUSINESS_OUTCOME` `MEMBER_NOT_FOUND` | 0 |
| `--param member_number=67890` | `BUSINESS_OUTCOME` `PERMISSION_DENIED` | 0 |

Both outcomes exit **0**. They are answers a caller branches on, not failures —
member `99999`'s run detects the outcome at step 6, the very step whose
checkpoint fails, and reports the answer instead of the crash. `HARD_FAILURE` and
`NEEDS_INTERVENTION` exit 1; exit **2** means the run never started, so the
environment is wrong rather than the flow — a missing browser, an off-allowlist
target, a parameter that fails its pattern.

Everything that can fail cheaply fails before the browser opens. A bad member
number costs milliseconds:

```bash
python -m cua replay lookup_member_balance --param member_number=abc
# Refusing to start: invalid parameters (1):
#   - member_number: 'abc' does not match ^[0-9]{5}$
```

### Mutations and the risk gate

```bash
python -m cua replay open_sub_account --param member_number=23456 --param account_type="Holiday Club" --param account_nickname="Vacation fund" --param initial_deposit=150.00 --approve-mutations
```

Thirteen steps, of which exactly one is `mutating` (step 11, "Proceed to review")
and one `irreversible` (step 12, "Confirm and finalize"). Without
`--approve-mutations` the run stops at step 11 with `MUTATION_NOT_APPROVED`. With
it, eleven steps complete and it stops at step 12 anyway, with
`IRREVERSIBLE_STEP`: standing approval is a statement about a *class* of actions,
never consent to one particular unrepeatable one. In both cases the sub-account
is genuinely not created — the gate stops the step, it does not report on one
that already ran, which the tests assert against the app's own state rather than
against the envelope.

### Injecting a runtime condition

`--chaos` arms one of the target app's flags from inside the run's own browser
session, mid-flow, just before the first click. It has to be the replay browser
that arms it, because the flags are session-scoped — a side-channel request would
arm a session nobody is driving. And it has to be mid-flow: armed on the opening
navigation every condition lands on the login page, where an 8s stall is absorbed
by the navigation timeout and a cleared session changes nothing, so the run goes
green and proves the opposite of what it claims.

```bash
python -m cua replay lookup_member_balance --param member_number=23456 --chaos slow
```

| Flag | What the engine does | Status |
|---|---|---|
| `--chaos slow` | per-attempt bound expires, re-checks (never re-sends), recovers | `SUCCESS` + `Recovered: SLOW_LOAD` |
| `--chaos session` | runs the declared re-login routine, re-verifies, continues | `SUCCESS` + `Recovered: SESSION_EXPIRED` |
| `--chaos dialog` | refuses to guess at an unrecognised modal | `NEEDS_INTERVENTION`, or a handoff — see below |
| `--chaos dialog`, modal declared in `runtime.yaml` | dismisses it, logs it, steps past | `SUCCESS` + `Recovered: KNOWN_INTERSTITIAL` |
| `--chaos error` | no retry — a 500 is the app saying it is broken | `HARD_FAILURE` |

A recovered condition is printed and logged in the trace but never becomes the
status: the caller asked whether the capability succeeded, not whether the
network had a bad afternoon. That is the three-class rule in DESIGN §6, and the
`slow` run is where you can watch it — `action_timeout`, then `recovered` with
`attempts: 2`, then `SUCCESS`.

Other flags: `--target` (default `http://127.0.0.1:5000`), `--headless`, and
`--screenshots all` for a capture per step rather than only on failure.

---

## Hand off to a human

Some things a run must not decide alone: an unrecognised dialog, a session that
expired with no way back, or a step that cannot be undone. When one happens and
there is an operator present, the run **pauses on the live browser session** — the
same window, never a fresh one — waits, and picks up where it left off.

```bash
# stops at step 12: the irreversible confirmation
python -m cua replay open_sub_account --param member_number=23456 --param "account_type=Holiday Club" --param "account_nickname=Vacation fund" --param initial_deposit=150.00 --approve-mutations

# stops at step 4: a maintenance modal nobody declared
python -m cua replay lookup_member_balance --param member_number=23456 --chaos dialog
```

You are told three ways, because the person is looking at the browser and not at
the terminal: a **banner across the live page** with the decision buttons on it, a
**rewritten tab title** (`[!] ACTION NEEDED`, which is what a minimised window
shows), and a **block in the terminal** with a bell. Answer by clicking the banner
or by typing at the prompt — whichever is nearer. Both stay live, because the page
is occasionally the reason someone was called.

The two pauses mean different things, and the prompt says which:

| | The step | Resuming means | You should |
|---|---|---|---|
| **Risk gate** | has *not* run | the automation performs it | approve — don't do it yourself |
| **Dialog / session** | already ran | the page is re-judged | fix it in the browser, then resume |

Nothing is taken on trust. Resuming re-checks the page rather than the answer, so
saying "resume" without having fixed anything gets you the same request back —
three times, then the run stops. And a step you approved that the automation then
can't perform, because you clicked it yourself, is detected by its checkpoint and
**not sent a second time**.

The result carries an `intervention_record`: who took over, at which step, what
the page did while they held control, when control came back, and whether the
checkpoint held afterwards. It appears on every terminal status, because a run
that stopped at a gate paused for a human just as much as one that was waved on.

`--no-console` refuses to pause at all and reports the intervention instead, which
is what an unattended run does. That is also automatic when stdin is not a
terminal — otherwise a run in CI would take an immediate EOF and report itself
abandoned by an operator who was never there.

Discovery escalates through the same path: when the agent calls `stuck`, a person
can unblock the page and the run carries on instead of ending.

---

## Validate a capability contract

An artifact is bytecode: replay executes it against a live system, so it is checked
on save and again on load. You can run that check by hand:

```bash
python -m cua validate capabilities/*.json
```

Two layers do the work. **JSON Schema** owns shape — including action-specific rules
that make a half-formed step impossible: a `navigate` with no checkpoint, a
`coordinates` strategy with nothing to verify against, a `read` that does not say
what it fills. **The validator** owns the cross-field facts JSON Schema cannot
express: that an output's `source_step` names the read step that actually fills it,
that `{inputs.member_number}` was declared, that step ids are contiguous. Every
problem is reported at once rather than one per run.

---

## Inside the target app

Reference for the sections above: how to provoke each condition by hand, and how
the app reports what it is.

### Chaos flags

Append to any page URL to arm a runtime condition. The arming page renders normally;
the flag fires on the **next** page load, once, then clears.

| URL | Effect | The condition it injects |
|---|---|---|
| `/search?chaos=slow` | 8s delay on the next page | `SLOW_LOAD` — recoverable by retry |
| `/search?chaos=session` | session expires, bounces to `/login` | `SESSION_EXPIRED` |
| `/search?chaos=dialog` | unexpected maintenance modal | `UNKNOWN_DIALOG` → intervention |
| `/search?chaos=error` | HTTP 500 page | `APP_ERROR` → hard failure |

Arm one, then look up `12345` to see it fire. If nothing happens, you probably loaded
two pages after arming and the first one consumed it.

### Drift

The app reports its own build — visible in the masthead, machine-readable as
`<meta name="generator" content="legacy-cu-portal@4.3.0">`. That is where an
artifact's `recorded_against.app_fingerprint` comes from, so replay can warn when a
recording has drifted from the surface it was compiled against.

Bump it without touching code:

```bash
# Windows
set TARGET_APP_BUILD=4.4.0 && python -m target_app
# macOS / Linux
TARGET_APP_BUILD=4.4.0 python -m target_app
```

---

## Tests

```bash
pytest                       # 347 tests, ~4 min
pytest -m "not browser"      # 269 tests, ~12s — no Chromium, no API key
pytest -m "not slow"         # skips the one that waits out the real 8s delay
```

**Run it sequentially.** `pytest -n auto` will produce failures that look like
engine bugs and are not: the target app keeps opened sub-accounts in module-level
state behind a single session-scoped server, and each chaos flag is armed once per
session and consumed by the next page load. Parallel workers share both and trample
each other. The suite is ~4 minutes as it is, which is cheaper than the afternoon
that misdiagnosis costs.

They cover the target app's determinism rules and hostile-markup guarantees, the
two contracts and their referential integrity, element distillation and the strategy
fallback chain against a real browser, the allowlist, risk classification,
redaction, and the discovery loop — the last with a faked surface and model, so a
test suite can never launch a browser or spend money on the API.

Replay and the handoff are tested end to end against the live app in a real
browser, which is affordable precisely because there is no model in that path:
every demo above is a test, including the recovered slow load, the re-login, the
unknown dialog, the two business outcomes, both risk gates, and an operator who
approves an irreversible step. `llm_call_count: 0` is asserted as a measured
property of a run, and the result schema refuses any other value when `mode` is
`replay`.

The handoff is testable at all because the operator console is an interface rather
than a `print` and an `input`. A scripted console can act on the live page exactly
as a person would — dismiss the modal, press the button — and then answer, which
puts the resume path under test rather than under a manual check the night before
a demo.

---

## Repo layout

```
target_app/           the fake portal — a prop, evaluated on nothing
  templates/          hostile markup lives here
cua/                  the system under evaluation
  contracts/          JSON Schemas + validator: artifact, result envelope
  executor/           surface abstraction, element distillation, actions
  agent/              discovery loop, the six tools, prompts
  distill/            trace → artifact
  policy/             allowlist, risk classification, redaction
  replay/             the interpreter: binding, checkpoints, outcomes, recovery
  hitl/               pause / cede control / resume, and the operator console
  config.py           model, stopping conditions, {secrets.*} resolution
  evidence.py         per-run directory, trace and transcript writers
policy.yaml           permitted origins and routes; per-step risk routes
outcomes.yaml         business outcomes declared per application
runtime.yaml          how to recognise a runtime condition on this surface
capabilities/         saved artifacts, one JSON per capability
evidence/             per-run traces, screenshots, transcripts
tests/
DESIGN.md             the build spec and source of truth
REPORT.md             the design write-up
FOUND_GAPS.md         what broke while building, and what was done about it
```

---

## Coming next

In rough order of value, and argued in [REPORT.md](REPORT.md) §7:

1. **A structural drift hash.** The fingerprint check catches changes someone
   remembered to announce. A tag-skeleton hash of the pages a capability touches
   catches the reordered table row that breaks a locator and leaves the build
   string untouched — turning drift from a postmortem finding into a pre-flight
   warning.
2. **Multi-run stability scoring.** Replay N times, report flakiness per
   capability and per strategy tier. That number is what would let unattended
   replay be gated on an approval state rather than on someone's judgement.
3. **A capability catalog.** Artifacts exposed as typed, callable capabilities an
   agent discovers by name rather than being handed a filename. The schema is
   already the tool definition, so most of this is plumbing.
4. **A second surface**, to find out how much of `distill/` is browser-shaped.
