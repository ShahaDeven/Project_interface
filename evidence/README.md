# Evidence

Thirteen recorded runs, in the order the story is meant to be read. Every one is
reproducible from `HEAD` with the command in its row — nothing here was staged, and
no configuration was changed to produce any of it.

The through-line: **a goal → an LLM-driven run that completes it → a saved artifact →
deterministic replay with new inputs, outcomes and errors → a human who takes over
the live session and hands it back.**

## What is in a run directory

- **`trace.jsonl`** — one JSON object per event, append-only: every step with the
  locator strategy that won and how long it took, every checkpoint, every outcome
  scan, recovery, and control transfer. This is where the interesting detail is.
- **`result.json`** — the run's result envelope (DESIGN §7): status, typed outputs,
  `llm_call_count`, and the status-specific payload a calling agent branches on.
- **`screenshots/`** — captured on failure, on outcome detection, and on
  intervention. Successful replays capture none by default; `--screenshots all`
  changes that.
- **`transcript.jsonl`** — *discovery only*: every model request and response,
  verbatim. Also `artifact.json`, the capability compiled from that run. Both
  discovery runs used **`claude-sonnet-5`**, and the model was told nothing about
  this application — no selectors, no page list, no hints. It is handed a goal, a
  screenshot and a numbered element list, and works the rest out.
- **`failure_stepNN.html`** on a hard failure, and **`human_stepNN_*_before/after.html`**
  around a handoff — DOM snapshots at the moment that mattered.

`run_id` and `evidence_path` inside `result.json` record the timestamped directory
each run wrote to. The directory names here are for humans; those fields are the
machine's record and were deliberately left untouched.

---

## The runs

| Run | What it demonstrates | Open this | Brief |
|---|---|---|---|
| **01_discover_lookup_member_balance** | A real LLM-driven run against a surface the model has never seen: it works out that the portal needs signing into, finds the search field by the text beside it, reads the balance. 8 model calls. | `transcript.jsonl` — the full conversation. The model is handed tokens, not values: it types `{secrets.operator_password}` and the **password never appears**, because a password field's value is never read during distillation. The **operator ID does appear** once typed — it is substituted the same way but reads back like any other text input. A non-secret identifier, and the boundary is deliberate: [FOUND_GAPS.md](../FOUND_GAPS.md) G-22. `artifact.json` is what got compiled out of this run. | §3.1 |
| **02_discover_open_sub_account** | The same, for the 13-step mutating flow. 14 model calls. | `artifact.json` — step 11 is `mutating`, step 12 `irreversible`, both classified from what the browser did, not from the model. | §3.1, §3.2 |
| **03_replay_success_23456** | The headline claim. Recorded against member `12345`, replayed against `23456`, **zero model calls**. | `result.json` — `"llm_call_count": 0` next to `"savings_balance": 18240.55` as a number, not a string. `trace.jsonl` — `strategy_used` on every step shows which tier of the fallback chain won. | §3.3 |
| **04_replay_member_not_found_99999** | "No such member" is an answer, not a crash. | `trace.jsonl` — `business_outcome` at step 6, and **no `hard_failure` anywhere in the file**. Step 6's checkpoint "Member Profile" did *not* hold; the outcome scan runs first, which is the whole point. | §3.3 |
| **05_replay_permission_denied_67890** | A region-scope denial, likewise a first-class result the caller branches on. | `result.json` — `outcome_code: PERMISSION_DENIED` with the screenshot that justifies it. Exit code 0: this is an answer. | §3.3 |
| **06_replay_recovered_slow_load** | An 8s stall is weather, not an outcome. Recovered, logged, and never a terminal status. | `trace.jsonl` — `action_timeout` at step 4, then `recovered` with `attempts: 2, action: "re-checked"`. Re-**checked**, never re-sent: a submitted form may be processing while the browser gives up waiting. | §3.3 |
| **07_replay_recovered_session_expired** | The session dies mid-flow and the run recovers, because a re-login routine is declared for this app. | `trace.jsonl` — `session_recovery_started`, three `recovery_step` events, then `recovered: SESSION_EXPIRED`. Those steps are ordinary artifact steps run by this same interpreter. | §3.3 |
| **08_replay_hard_failure_app_error** | A 500 is the app saying it is broken. No retry — asking again is not a plan. | `result.json` — expected, observed, the strategies tried, and a DOM snapshot. Deliberately **no remediation field**: if the engine knew the fix it would be a recoverable condition. | §3.3, §3.5 |
| **09_replay_gate_mutation_not_approved** | Stops *before* the mutating step when the invocation carries no standing approval. | `result.json` — `steps_completed: 10` of 13, `reason: MUTATION_NOT_APPROVED`, `control: HUMAN`. The gate stopped the step; it did not report on one that already ran. | §3.4 |
| **10_replay_gate_irreversible_step** | Run with `--approve-mutations` and it still stops at step 12. Standing approval is a statement about a class of actions, never consent to one unrepeatable one. | `result.json` — `steps_completed: 11`, the mutating step ran, the irreversible one did not. | §3.4 |
| **11_replay_handoff_unknown_dialog** | An unrecognised modal on a banking screen. The system refuses to guess, hands over the live session, and the run completes after a person deals with it. | `trace.jsonl` — `unknown_dialog` → `paused_for_human` → `control_transferred` → `human_action` → `control_returned` → `resuming`. `result.json` — `intervention_record` with `resolution: verified`. | §3.6 |
| **12_replay_handoff_irreversible_approved** | The intended shape of a risk-gate handoff: the human approves, **the automation performs the step**. | `result.json` — `intervention_record`, `when: before_step`, `decision: resume`, `resolution: verified`, and `post_resume_checkpoint` showing the state was re-checked before continuing. No recovery: nothing went wrong. | §3.6 |
| **13_replay_handoff_operator_clicked_confirm** | The same run, except the operator pressed the application's Confirm button themselves before approving. The automation found its target gone, checked the step's own checkpoint, and **did not send it again**. | `trace.jsonl` — `performed_by_human`, then `recovered: HUMAN_PERFORMED_STEP, action: "not re-sent"`. One sub-account exists, not two. The story is in [FOUND_GAPS.md](../FOUND_GAPS.md) **G-20**. | §3.6 |

---

## Reading it in ten minutes

If you only open three: **03** for the central claim, **04** for the business-outcome
boundary, and **13** for the handoff.

**A note on what is not here.** `KNOWN_INTERSTITIAL` — a *declared* dialog being
dismissed automatically — has no run in this set. This app raises exactly one modal,
and it is deliberately left undeclared in [`runtime.yaml`](../runtime.yaml) so that
run 11 can exist at all; declaring it would make the escalation path
undemonstrable. Recording it would have meant shipping evidence whose configuration
does not match `HEAD`, which is worse than a missing row. It is covered instead by
`test_a_declared_interstitial_is_dismissed_and_the_run_continues` in
[`tests/test_replay.py`](../tests/test_replay.py), which sits directly beside
`test_an_unknown_dialog_stops_and_hands_over`: the same modal, the same page,
opposite outcomes, with a config entry the only difference.

Runs 01 and 02 are **frozen**. They are the provenance of the two committed
capabilities — `recorded_against.discovery_run_id` in each artifact points at them —
so re-recording discovery would produce a different artifact and turn every replay
above into evidence for a capability that no longer exists.
