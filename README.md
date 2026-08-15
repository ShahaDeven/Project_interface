# Computer-Use Automation System

An LLM discovers how to complete a task in a legacy web app once, and the run is
distilled into a reusable **capability artifact**. After that the task is executed by
a deterministic interpreter with **zero LLM calls** — new inputs, same flow.

Discovery is a compiler. Replay is a VM. The artifact is the bytecode.

See [DESIGN.md](DESIGN.md) for the full spec — it is the source of truth, and this
README only tells you how to run things.

---

## Build status

| Piece | State |
|---|---|
| `target_app/` — the fake operator portal (DESIGN §2) | **done** |
| `cua/contracts/` — artifact + result JSON Schemas (§5, §7) | **done** |
| `cua/executor/` — perception, actions, allowlist (§3, §8) | **done** |
| `cua/agent/` — discovery loop, six tools (§4) | **done** |
| `cua/distill/` — trace → artifact | **done** |
| `cua/policy/` — allowlist, risk classification, redaction (§8) | **done** |
| `cua/replay/` — the interpreter (§6) | **done** |
| `cua/hitl/` — escalation and handoff (§9) | not started |

286 tests cover what is done, and two capabilities have been recorded from real
LLM-driven runs.

Replay is complete: input binding, the strategy fallback chain, per-step
checkpoints, the outcome scanner, runtime-condition recovery and the risk gates.
What remains is the far side of an escalation. Today a run that needs a human
stops and hands back a `NEEDS_INTERVENTION` envelope naming the live browser
session; it cannot yet pause, wait for that person, capture what they did, and
resume. The trigger is real and the payload is real — the seam in §9 is what is
missing.

---

## Requirements

- **Python 3.11+**
- A browser binary for Playwright (one extra command, see below) — needed from Day 2.

## Setup

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
playwright install chromium        # separate download; nothing runs without it
```

`pip install playwright` only fetches the Python client. The Chromium binary is a
separate ~140MB download, which is what `playwright install chromium` fetches.

### Credentials

Copy `.env.example` to `.env` (gitignored) and fill it in:

```
ANTHROPIC_API_KEY=sk-ant-...
CUA_SECRET_OPERATOR_ID=e.okafor
CUA_SECRET_OPERATOR_PASSWORD=not-a-real-password
```

The key is needed for **discovery only** — replay makes no model calls. The two
`CUA_SECRET_*` values are the operator credentials a capability declares in
`requires_secrets`. The model never receives them: it types the token
`{secrets.operator_id}` and the executor substitutes the value at the keystroke, so
the credential never enters a transcript, a trace, or an artifact. The target app
accepts anything; these exist so the mechanism is real rather than mocked.

---

## Record a capability

With the target app running (see below), this is a genuine LLM-driven run — the
model has never seen this app and is told nothing about it:

```bash
python -m cua discover \
  --goal "look up member 12345 and read their savings balance" \
  --target http://127.0.0.1:5000 --save-as lookup_member_balance
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
checkpoint fails, and reports the answer instead of the crash. Only
`HARD_FAILURE` and `NEEDS_INTERVENTION` exit 1.

Everything that can fail cheaply fails before the browser opens. A bad member
number costs milliseconds:

```bash
python -m cua replay lookup_member_balance --param member_number=abc
# Refusing to start: invalid parameters (1):
#   - member_number: 'abc' does not match ^[0-9]{5}$
```

### Mutations and the risk gate

```bash
python -m cua replay open_sub_account --param member_number=23456 \
  --param account_type="Holiday Club" --param account_nickname="Vacation fund" \
  --param initial_deposit=150.00 --approve-mutations
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
| `--chaos dialog` | refuses to guess at an unrecognised modal | `NEEDS_INTERVENTION` |
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

## Run the target app

```bash
python -m target_app          # or: python -m cua target_app serve
```

Serves **http://127.0.0.1:5000**. Ctrl+C to stop.

It is a deliberately hostile 2003-era credit union operator portal: nested-table
layout, no `id` attributes, no `data-*` hooks, no semantic HTML5, generic class names
reused for unrelated things, form fields identified only by adjacent `<td>` text, and
loan details buried in an unnamed `<iframe>`. That is the point — it is the surface
the automation has to cope with. It is a prop, and it is evaluated on nothing.

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
pytest                       # 286 tests, ~3 min
pytest -m "not browser"      # skips the ones driving real Chromium
pytest -m "not slow"         # skips the one that waits out the real 8s delay
```

They cover the target app's determinism rules and hostile-markup guarantees, the
two contracts and their referential integrity, element distillation and the strategy
fallback chain against a real browser, the allowlist, risk classification,
redaction, and the discovery loop — the last with a faked surface and model, so a
test suite can never launch a browser or spend money on the API.

Replay is tested end to end against the live app in a real browser, which is
affordable precisely because there is no model in that path: every demo below is
a test, including the recovered slow load, the re-login, the unknown dialog, the
two business outcomes and both risk gates. `llm_call_count: 0` is asserted as a
measured property of a run, and the result schema refuses any other value when
`mode` is `replay`.

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
  hitl/               pause / cede control / resume (Day 4)
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
```

---

## Coming next

The human-handoff seam (DESIGN §9). Every trigger into it already works —
`--chaos dialog` produces a live `NEEDS_INTERVENTION` naming the browser session,
and both risk gates produce one on demand — but the run currently ends there
instead of pausing.

What is missing is the far side: the `RUNNING → PAUSED_FOR_HUMAN → RESUMING`
state machine, the terminal operator prompt, capture of what the person did while
control was theirs, re-verification of the checkpoint before control returns
(never a blind resume), and the `intervention_record` on the final result. The
console is deliberately mocked; the mechanism is not — the human takes over the
same live Playwright session, which is why the session id is in the envelope
today rather than a placeholder for one.

Then the committed evidence set (DESIGN §10) and REPORT.md.
