"""Human-in-the-loop escalation and handoff.

The state machine RUNNING → PAUSED_FOR_HUMAN → RESUMING → terminal, the terminal
operator prompt, and the capture of what the human did while control was theirs.
The operator console is deliberately mocked; the handoff mechanism is not — the
human operates the same live browser session, and the engine re-verifies state
before resuming rather than blind-resuming.

Not implemented yet — Day 4.
"""
