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
| `cua/agent/` — discovery loop (§4) | not started |
| `cua/distill/` — trace → artifact | not started |
| `cua/replay/` — the interpreter (§6) | not started |
| `cua/policy/`, `cua/hitl/` — guardrails, handoff (§8, §9) | not started |

120 tests cover what is done. Everything below the second row is Days 2–4, and the
commands in [Coming next](#coming-next) do not work yet — they exit 2 with a
message rather than failing obscurely.

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

---

## Validate a capability contract

An artifact is bytecode: replay executes it against a live system, so it is checked
on save and again on load. You can run that check by hand:

```bash
python -m cua validate tests/fixtures/lookup_member_balance.json
```

Two layers do the work. **JSON Schema** owns shape — including action-specific rules
that make a half-formed step impossible: a `navigate` with no checkpoint, a
`coordinates` strategy with nothing to verify against, a `read` that does not say
what it fills. **The validator** owns the cross-field facts JSON Schema cannot
express: that an output's `source_step` names the read step that actually fills it,
that `{inputs.member_id}` was declared, that step ids are contiguous. Every problem
is reported at once rather than one per run.

The fixture doubles as a worked example of the schema, recorded against the target
app in this repo.

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
`<meta name="generator" content="legacy-cu-portal@4.2.1">`. That is where an
artifact's `recorded_against.app_fingerprint` comes from, so replay can warn when a
recording has drifted from the surface it was compiled against.

Bump it without touching code:

```bash
# Windows
set TARGET_APP_BUILD=4.3.0 && python -m target_app
# macOS / Linux
TARGET_APP_BUILD=4.3.0 python -m target_app
```

---

## Tests

```bash
pytest                    # 73 tests, ~10s
pytest -m "not slow"      # skips the one test that waits out the real 8s delay
```

They cover the region and not-found rules, the mutation flow, every chaos flag, the
self-reported build, and the hostile-markup rules on every page — the app is a prop,
but the artifacts recorded against it depend on it behaving exactly as specified.

---

## Repo layout

```
target_app/           the fake portal — a prop, evaluated on nothing
  templates/          hostile markup lives here
cua/                  the system under evaluation
  contracts/          JSON Schemas + validator: artifact, result envelope
  agent/              discovery loop (Day 2)
  distill/            trace → artifact (Day 2)
  replay/             the interpreter (Day 3)
  policy/             allowlist, risk gates, redaction (Day 3)
  hitl/               pause / cede control / resume (Day 4)
capabilities/         saved artifacts, one JSON per capability
evidence/             per-run traces, screenshots, transcripts
tests/
DESIGN.md             the build spec and source of truth
REPORT.md             the design write-up
```

---

## Coming next

Not yet implemented — listed so the intended shape is visible (DESIGN §12):

```bash
python -m cua discover --goal "look up member 12345 and read their savings balance" \
       --target http://localhost:5000 --save-as lookup_member_balance

python -m cua replay lookup_member_balance --param member_id=23456
python -m cua replay lookup_member_balance --param member_id=99999
python -m cua replay open_sub_account --param member_id=23456 --approve-mutations
```
