"""Human-in-the-loop escalation and handoff (DESIGN §9).

The state machine RUNNING → PAUSED_FOR_HUMAN → RESUMING → terminal, the terminal
operator prompt, and the control flag that keeps one writer on the page at a time.
The operator console is deliberately mocked; the handoff mechanism is not — the
human operates the same live browser session, and the engine re-verifies state
before resuming rather than blind-resuming.

The console is an interface so the whole path is testable without a person at a
keyboard: `ScriptedConsole` can act on the live page exactly as an operator would
and then answer, which is what makes the resume path — the part most likely to be
wrong — the part that gets tested most.

Still to come (4b): capture of what the human did while control was theirs, as
`human_action` trace entries, and the `intervention_record` on the final envelope.
`Handoff.pauses` already accumulates what that record is built from.
"""

from .console import (ABANDON, RESUME, InterventionRequest, ScriptedConsole,
                      TerminalConsole)
from .handoff import (AUTOMATION, HUMAN, PAUSED_FOR_HUMAN, RESUMING, RUNNING,
                      ControlViolation, Handoff)

__all__ = [
    "Handoff", "ControlViolation",
    "InterventionRequest", "TerminalConsole", "ScriptedConsole",
    "RESUME", "ABANDON",
    "RUNNING", "PAUSED_FOR_HUMAN", "RESUMING", "AUTOMATION", "HUMAN",
]
