"""The handoff state machine (DESIGN §9).

    RUNNING → PAUSED_FOR_HUMAN → RESUMING → (terminal)

Three things live here rather than in the engine, because they are true of a
handoff regardless of what was being executed when it happened:

**Control is tracked explicitly, not implied.** A single writer at a time is the
invariant that makes a live handoff safe — the human and the automation driving
the same page at once is how two clicks land on one confirmation button. So the
flag is real state with a guard behind it, and the engine asks before it acts.

**The attention channels are the handoff's job, not the console's.** The banner on
the live page has to go up when control transfers and come down before the engine
looks at that page again, and only this object knows both moments. It also has to
come down *before* re-verification: the banner is a fixed-position element, which
is exactly what `runtime.yaml` uses to recognise a modal, so a banner still on
screen when the engine re-scans reads as an UNKNOWN_DIALOG and pauses the run a
second time over a message we wrote ourselves.

**Pauses are bounded per step.** An operator can answer "resume" without having
changed anything — deliberately, or because they misread what was asked. The
engine re-verifies and pauses again, which is correct and would loop forever. After
a few rounds the run stops and says so, because a third identical request is
information about the situation, not about the page.
"""

import hashlib
from pathlib import Path

from ..evidence import utc_now
from .console import ABANDON, RESUME

RUNNING = "RUNNING"
PAUSED_FOR_HUMAN = "PAUSED_FOR_HUMAN"
RESUMING = "RESUMING"

AUTOMATION = "AUTOMATION"
HUMAN = "HUMAN"


class ControlViolation(Exception):
    """The automation tried to act while a human held control.

    Never expected to fire in a correct run, which is why it is loud: the whole
    point of tracking control explicitly is that the invariant is checkable.
    """


class Handoff:

    max_pauses_per_step = 3

    def __init__(self, surface, evidence, console=None):
        self.surface = surface
        self.evidence = evidence
        self.console = console
        self.state = RUNNING
        self.control = AUTOMATION
        self.pauses = []
        self._per_step = {}

    # ------------------------------------------------------------- control --

    @property
    def held_by_human(self):
        return self.control == HUMAN

    def check_may_act(self, what=""):
        """Called by the engine before it touches the surface."""
        if self.held_by_human:
            raise ControlViolation(
                f"automation attempted {what or 'an action'} while control is HUMAN; "
                f"a live handoff has one writer at a time")

    @property
    def available(self):
        """Whether there is anyone to hand control *to*.

        Absent a console the pause is still real and still reported — it just
        becomes terminal, which is exactly the behaviour before any of this
        existed. An unattended run does not get to approve its own mutations.
        """
        return bool(self.console and self.console.available())

    # --------------------------------------------------------------- pause --

    def offer(self, request):
        """Hand control to a human and block until they answer.

        Returns RESUME or ABANDON. Every transition is traced, because the
        question a reviewer asks about an intervention is not "did it pause" but
        "who was in control, when, and what happened while they were".
        """
        if not self.available:
            self.evidence.trace("no_operator_console", step=request.step,
                                reason=request.reason)
            return ABANDON

        seen = self._per_step.get(request.step, 0)
        if seen >= self.max_pauses_per_step:
            self.evidence.trace("handoff_exhausted", step=request.step,
                                pauses=seen, reason=request.reason)
            return ABANDON
        self._per_step[request.step] = seen + 1

        attempt = seen + 1
        self.state = PAUSED_FOR_HUMAN
        self.control = HUMAN
        started = utc_now()
        self.evidence.trace("control_transferred", to=HUMAN, step=request.step,
                            reason=request.reason, when=request.when, attempt=attempt)

        # Observed before the banner goes up, so the evidence shows the page as
        # the operator found it rather than as the engine decorated it.
        before = self._observe(f"step{request.step:02d}_{attempt}_before")
        self._banner_up(request)

        decision = ABANDON
        try:
            decision = self.console.ask(request, watch=self._page_decision)
        finally:
            # Down before anything else looks at the page: the banner is a
            # fixed-position element and would otherwise be scanned as a modal.
            self._banner_down()
            self.control = AUTOMATION
            self.state = RESUMING if decision == RESUME else RUNNING
            returned = utc_now()
            after = self._observe(f"step{request.step:02d}_{attempt}_after")
            action = self._transition(request, attempt, before, after)
            self.pauses.append({
                "step": request.step,
                "reason": request.reason,
                "when": request.when,
                "requested_action": request.requested_action,
                "paused_at": started,
                "control_returned_at": returned,
                "decision": decision,
                # Filled by `resolve` once the engine has re-judged the page.
                # Abandoned handoffs never get there, and say so.
                "resolution": None if decision == RESUME else "not_resumed",
                "human_actions": [action] if action else [],
            })
            self.evidence.trace("control_returned", to=AUTOMATION, step=request.step,
                                decision=decision, paused_at=started,
                                control_returned_at=returned)
        return decision

    def resolve(self, step_id, resolution, checkpoint=None):
        """Record how the most recent handoff on this step ended.

        Only the newest unresolved pause is filled, so a step that paused three
        times keeps three distinct outcomes rather than one overwritten one.
        """
        for pause in reversed(self.pauses):
            if pause["step"] == step_id and pause.get("resolution") is None:
                pause["resolution"] = resolution
                if checkpoint:
                    pause["post_resume_checkpoint"] = checkpoint
                return pause
        return None

    def record(self):
        """The `intervention_record` for the result envelope (§7).

        Every handoff, in order. A run that paused twice reports twice: discarding
        one loses the entry an audit exists to keep — a human approving a write to
        a member's account.
        """
        interventions = []
        for pause in self.pauses:
            entry = {
                "paused_at_step": pause["step"],
                "reason": pause["reason"],
                "when": pause["when"],
                "requested_action": pause["requested_action"],
                "paused_at": pause["paused_at"],
                "control_returned_at": pause["control_returned_at"],
                "decision": pause["decision"],
                "resolution": pause.get("resolution") or "not_resumed",
                "human_actions": pause["human_actions"],
            }
            if pause.get("post_resume_checkpoint"):
                entry["post_resume_checkpoint"] = pause["post_resume_checkpoint"]
            interventions.append(entry)

        record = {"interventions": interventions}
        operator = getattr(self.console, "operator", None)
        if operator:
            record["operator"] = operator
        return record

    # ------------------------------------------------------------ capture --

    def _observe(self, tag):
        """URL and DOM at one moment, written to evidence.

        What gets captured is the page's *state*, not the operator's keystrokes.
        Two reasons, and the second is the binding one: the engine is blocked on
        the console while they work, so it has no opportunity to watch; and
        instrumenting the page to record what they typed would put credentials
        into the trace, which §8 makes structurally impossible everywhere else.
        A before/after pair says what changed, which is what an audit needs.
        """
        if self.surface is None:
            return {}
        state = {"at": utc_now(), "url": "", "digest": ""}
        try:
            state["url"] = self.surface.current_url()
        except Exception:
            pass
        try:
            written = Path(self.surface.dom_snapshot(
                Path(self.evidence.dir) / f"human_{tag}.html"))
            # Hashed from where it was written, recorded from where a reader will
            # look for it. The two are the same file and rarely the same string.
            state["digest"] = hashlib.sha1(written.read_bytes()).hexdigest()[:12]
            state["dom"] = self.evidence.relative(written)
        except Exception:
            # A page that cannot be serialised — closed, mid-navigation — is worth
            # noting and never worth ending a handoff over.
            pass
        return state

    def _transition(self, request, attempt, before, after):
        """One `human_action`: what the page did while control was theirs."""
        if not before and not after:
            return None

        if before.get("url") != after.get("url"):
            summary = (f"navigated from {before.get('url') or '?'} "
                       f"to {after.get('url') or '?'}")
        elif before.get("digest") != after.get("digest"):
            summary = "changed the page without navigating"
        else:
            # Not an error: the operator may have judged that nothing was needed.
            # It is also what a blind resume looks like, which is why the engine
            # re-verifies rather than taking the answer as evidence.
            summary = "no observable change"

        self.evidence.trace("human_action", step=request.step, attempt=attempt,
                            url_before=before.get("url"), url_after=after.get("url"),
                            dom_before=before.get("dom"), dom_after=after.get("dom"),
                            changed=summary != "no observable change",
                            summary=summary)
        return {"recorded_at": after.get("at") or utc_now(),
                "url": after.get("url") or "",
                "summary": summary}

    def resumed(self, step_id):
        self.state = RUNNING
        self.evidence.trace("resuming", step=step_id)

    # ------------------------------------------------------------- banner --

    @property
    def _page_decision(self):
        """Poll for a decision made on the page, when the surface offers one.

        None when it does not, which puts the console back on the plain blocking
        prompt — a surface with no way to paint buttons must not leave the
        operator with no way to answer.
        """
        return getattr(self.surface, "banner_decision", None)

    def _banner_up(self, request):
        show = getattr(self.surface, "show_banner", None)
        if show is None:
            return
        show(request.headline(), request.banner_lines(), title=request.page_title(),
             choices=request.banner_choices() if self._page_decision else ())

    def _banner_down(self):
        clear = getattr(self.surface, "clear_banner", None)
        if clear is not None:
            clear()
