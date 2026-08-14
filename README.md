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
| `cua/replay/` — the interpreter (§6) | not started |
| `cua/hitl/` — escalation and handoff (§9) | not started |

225 tests cover what is done, and two capabilities have been recorded from real
LLM-driven runs. `replay` is the remaining core piece; it exits 2 with a message
rather than failing obscurely.

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
pytest                       # 225 tests, ~1 min
pytest -m "not browser"      # skips the ones driving real Chromium
pytest -m "not slow"         # skips the one that waits out the real 8s delay
```

They cover the target app's determinism rules and hostile-markup guarantees, the
two contracts and their referential integrity, element distillation and the strategy
fallback chain against a real browser, the allowlist, risk classification,
redaction, and the discovery loop — the last with a faked surface and model, so a
test suite can never launch a browser or spend money on the API.

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
  replay/             the interpreter (Day 3)
  hitl/               pause / cede control / resume (Day 4)
  config.py           model, stopping conditions, {secrets.*} resolution
  evidence.py         per-run directory, trace and transcript writers
policy.yaml           permitted origins and routes; per-step risk routes
outcomes.yaml         business outcomes declared per application
capabilities/         saved artifacts, one JSON per capability
evidence/             per-run traces, screenshots, transcripts
tests/
DESIGN.md             the build spec and source of truth
REPORT.md             the design write-up
```

---

## Coming next

Replay is the production path: the same artifact, new inputs, **zero model calls**.
Not yet implemented — listed so the intended shape is visible (DESIGN §12):

```bash
python -m cua replay lookup_member_balance --param member_number=23456
python -m cua replay lookup_member_balance --param member_number=99999
python -m cua replay open_sub_account --param member_number=23456 \
       --param account_type="Holiday Club" --param account_nickname="Vacation fund" \
       --param initial_deposit=150.00 --approve-mutations
```

Input names are derived from the field's on-screen label rather than authored, so
`Member number` becomes `member_number`. The same artifact is meant to produce a
different ending per input: `23456` succeeds with typed outputs, `99999` returns
`MEMBER_NOT_FOUND` and `67890` returns `PERMISSION_DENIED` — both business outcomes
a caller branches on, not failures.
