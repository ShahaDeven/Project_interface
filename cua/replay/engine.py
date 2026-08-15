"""The artifact interpreter — the production execution path (DESIGN §6, §7).

Discovery is a compiler; this is the VM. It walks a saved artifact with no model in
the decision loop, which is the system's central claim and is enforced rather than
asserted: the result envelope declares `mode: "replay"`, and the schema constrains
that to `llm_call_count: 0`. A replay result carrying a model call is not a warning,
it is an invalid document.

Three things this file is careful about:

**The recorded chain is executed, not regenerated.** Each step's strategies are
passed to the executor exactly as the artifact holds them, in order, and the one
that resolved is logged. Rebuilding the chain from the live page would quietly
repair drift instead of reporting it — and the whole value of the fallback chain is
knowing which tier is carrying the weight.

**A checkpoint is verified before the next step runs.** "The click worked" is never
assumed, so a failure is attributed to the step that caused it rather than surfacing
three steps later as an unresolvable target.

**Risk is gated before the action, not after.** A step that must not run does not
run; the engine stops and hands back a live intervention request.

**A recoverable condition never reaches the caller as a status.** Slow loads,
session expiry and declared interstitials are handled in the loop around
`_verify` and recorded in the trace; the caller asked whether the capability
succeeded, not whether the network had a bad afternoon.
"""

import time
from dataclasses import dataclass, field

from ..evidence import RunEvidence, utc_now
from ..executor import ActionTimeout, TargetNotFound
from ..executor.surface import Element
from ..policy import PolicyViolation
from .binding import ParseError, bind_inputs, parse_value, resolve

PAGE_ACTIONS = ("navigate", "click")


class StepFailure(Exception):
    """A step could not complete. Carries what §7 needs for forensics."""

    def __init__(self, expected, observed, strategies_tried=()):
        super().__init__(observed)
        self.expected = expected
        self.observed = observed
        self.strategies_tried = list(strategies_tried)


class RiskRefused(Exception):
    """A step's risk class forbids running it under this invocation's approvals."""

    def __init__(self, reason, detail, requested_action):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.requested_action = requested_action


@dataclass
class ReplayResult:
    status: str
    run_id: str
    evidence_path: str
    outputs: dict = field(default_factory=dict)
    envelope: dict = field(default_factory=dict)
    recoveries: list = field(default_factory=list)

    @property
    def succeeded(self):
        return self.status == "SUCCESS"


def element_for(target):
    """Rebuild an addressable element from a recorded target descriptor.

    The same `Element` type flows through discovery and replay, so the executor's
    action code is shared rather than duplicated — a capability cannot pass
    discovery and fail replay because the two used different machinery to click.
    """
    strategies = target.get("strategies", [])
    by_kind = {strategy["kind"]: strategy for strategy in strategies}
    label = by_kind.get("label", {})
    coordinates = by_kind.get("coordinates", {}).get("value", [0, 0])
    role = label.get("role") or "control"
    return Element(
        index=-1,
        kind="text" if role == "cell" else "interactive",
        role=role,
        label=label.get("value", ""),
        structural=by_kind.get("structural", {}).get("value", ""),
        center=tuple(coordinates),
        box=(0, 0, 0, 0),
        frame=target.get("frame"),
    )


class ReplayEngine:

    def __init__(self, surface, artifact, evidence=None, runtime=None,
                 approve_mutations=False, screenshots="failure", chaos=None):
        self.surface = surface
        self.artifact = artifact
        self.evidence = evidence or RunEvidence()
        self.runtime = runtime
        self.approve_mutations = approve_mutations
        self.screenshots = screenshots
        self.chaos = chaos
        self._chaos_armed = False
        self._credentials_submitted = False
        self._timed_out_at = None
        self.outputs = {}
        self.steps_completed = 0
        self.strategy_log = []
        self.recoveries = []

    # ------------------------------------------------------------------ run --

    def run(self, params, base_url):
        capability = self.artifact["capability"]
        started_at = utc_now()

        # Everything that can fail cheaply fails before the browser is touched.
        inputs = bind_inputs(self.artifact, params)

        if self.runtime:
            self.surface.set_timeout(self.runtime.step_timeout_ms,
                                     self.runtime.settle_timeout_ms)

        self.evidence.trace("run_started", mode="replay",
                            capability=capability["name"],
                            capability_version=capability["version"],
                            inputs=inputs, base_url=base_url,
                            approve_mutations=self.approve_mutations)

        steps = self.artifact["steps"]
        fingerprint_seen = None
        outcome = None

        for step in steps:
            try:
                self._gate(step)
                self._maybe_arm_chaos(step, base_url)
                try:
                    self._execute(step, inputs, base_url)
                except ActionTimeout as slow:
                    # Not a failure, and explicitly not a reason to re-send: the
                    # action may have gone through and be processing. Fall through
                    # to checkpoint verification, whose retry loop decides whether
                    # the page merely arrived late.
                    self._timed_out_at = step["id"]
                    self.evidence.trace("action_timeout", step=step["id"],
                                        detail=str(slow))
                self.steps_completed += 1
                outcome = self._settle(step, inputs, base_url)
                if outcome:
                    break
                continue
            except RiskRefused as refusal:
                outcome = self._intervention(step, refusal)
                break
            except (StepFailure, TargetNotFound, PolicyViolation, ParseError) as failure:
                outcome = self._failure(step, failure)
                break
            finally:
                if fingerprint_seen is None:
                    fingerprint_seen = self.surface.app_fingerprint()
        else:
            outcome = self._finish()

        envelope = self._envelope(outcome, started_at, inputs, fingerprint_seen)
        self.evidence.write_json("result.json", envelope)
        self.evidence.trace("run_finished", status=envelope["status"],
                            steps_completed=self.steps_completed,
                            recoveries=self.recoveries)
        return ReplayResult(status=envelope["status"], run_id=self.evidence.run_id,
                            evidence_path=self.evidence.path,
                            outputs=self.outputs, envelope=envelope,
                            recoveries=self.recoveries)

    # ----------------------------------------------------------- one step --

    def _gate(self, step):
        """Risk gates (§8). Conservative by design, and stated as such in REPORT.

        `irreversible` pauses even with standing approval: a blanket
        `--approve-mutations` is a statement about a class of actions, not consent
        to any particular unrepeatable one.
        """
        risk = step["risk"]
        if risk == "irreversible":
            raise RiskRefused(
                "IRREVERSIBLE_STEP",
                f"Step {step['id']} ({step['action']}) is irreversible and always "
                f"requires explicit human confirmation.",
                "Confirm this specific action, or abandon the run.")
        if risk == "mutating" and not self.approve_mutations:
            raise RiskRefused(
                "MUTATION_NOT_APPROVED",
                f"Step {step['id']} ({step['action']}) changes state and this "
                f"invocation carries no standing approval.",
                "Re-invoke with --approve-mutations, or approve this step.")

    def _execute(self, step, inputs, base_url, event="step"):
        action = step["action"]
        started = time.monotonic()
        self.surface.last_strategy = None
        timeout = None

        try:
            if action == "navigate":
                self.surface.navigate(resolve(step["url"], inputs, base_url))
            else:
                target = step["target"]
                element = element_for(target)
                strategies = target["strategies"]
                if action == "click":
                    self.surface.click(element, strategies=strategies)
                elif action == "type":
                    if "{secrets." in (step.get("value") or ""):
                        self._credentials_submitted = True
                    self.surface.type(element, resolve(step["value"], inputs, base_url),
                                      strategies=strategies)
                else:
                    self._extract(step, element, strategies)
        except ActionTimeout as slow:
            # Held, not swallowed. A step that exceeded its bound still *ran* —
            # the click landed, the form may be processing — so it belongs in the
            # trace exactly once, like any other step. Letting the exception skip
            # the record is how a step goes missing from the trace of the run that
            # most needed one, and how "every step ran exactly once" becomes
            # unverifiable precisely when re-sending is the tempting mistake.
            timeout = slow

        strategy = getattr(self.surface, "last_strategy", None)
        if strategy:
            self.strategy_log.append(strategy)

        self.evidence.trace(event, step=step["id"], action=action, risk=step["risk"],
                            strategy_used=strategy, timed_out=timeout is not None,
                            duration_ms=int((time.monotonic() - started) * 1000))

        if timeout:
            # No screenshot here: the page is mid-flight by definition, and the
            # verification loop below is what decides whether this mattered.
            raise timeout

        if self.screenshots == "all" and event == "step":
            self.surface.screenshot(self.evidence.screenshot_path(f"step{step['id']:02d}"))

    def _maybe_arm_chaos(self, step, base_url):
        """Arm a runtime condition just before the first click, mid-flow.

        Demo scaffolding, and the only place the engine knows the target app has
        such a thing. Two details it took a broken run to get right:

        It is armed *mid-flow* rather than on the opening navigation. Armed at the
        start, the condition lands on the login page, where an 8s stall is absorbed
        by the navigation timeout and a cleared session changes nothing — the run
        goes green and proves the opposite of what it claims.

        It is armed with a background request rather than a navigation, because
        navigating would discard the page state the flow has already built.
        """
        if not self.chaos or self._chaos_armed or step["action"] != "click":
            return
        self._chaos_armed = True
        # Re-request the page we are already on. Any other URL risks a redirect,
        # and a redirect is a second page load that would consume the flag before
        # the flow ever reaches it — which is exactly what a fixed /search URL did.
        current = self.surface.current_url()
        separator = "&" if "?" in current else "?"
        self.surface.background_request(f"{current}{separator}chaos={self.chaos}")
        self.evidence.trace("chaos_armed", mode=self.chaos, before_step=step["id"])

    def _extract(self, step, element, strategies):
        name = step["output"]
        declaration = self.artifact["outputs"][name]
        raw = self.surface.read(element, strategies=strategies)
        try:
            self.outputs[name] = parse_value(raw, declaration.get("parse", "text"))
        except ParseError as error:
            raise StepFailure(
                f"a value readable as {declaration.get('parse', 'text')} for '{name}'",
                str(error), self.strategy_log[-1:]) from None

    def _settle(self, step, inputs, base_url):
        """Decide what the page now showing actually means (§6).

        The order is the design. A declared business outcome wins over everything,
        because it is an answer the caller asked for. An error page is next, and is
        never retried — a 500 is the app saying it is broken, and asking again is
        not a plan. Dialogs are then dealt with before the checkpoint is judged,
        since a modal can leave the underlying page's text intact and make a
        checkpoint pass over the top of something that has blocked the flow.

        Returns a terminal outcome, or None to continue.
        """
        text = self.surface.page_text()
        url = self.surface.current_url()

        declared = self._match_outcome(text, url)
        if declared:
            return self._business_outcome(step, declared)

        if self.runtime and self._matches_any(self.runtime.app_error, text, url):
            return self._failure(step, StepFailure(
                "a working application page",
                "the application returned an error page",
                self.strategy_log[-1:]))

        blocked = self._handle_dialog(step)
        if blocked:
            return blocked

        return self._verify(step, inputs, base_url)

    def _handle_dialog(self, step):
        """Declared interstitials are dismissed and logged; unknown ones stop.

        Never guess at a dialog we did not expect — an unrecognised modal on a
        banking screen is exactly the situation where clicking the most plausible
        button is how automation does damage.
        """
        if not self.runtime or not self.runtime.detects_dialogs:
            return None
        dialog = self.surface.dialog_present(self.runtime.dialogs.detect_selector)
        if dialog is None:
            return None

        known = self.runtime.dialogs.identify(dialog)
        if known is None:
            screenshot = self.surface.screenshot(
                self.evidence.screenshot_path(f"intervention_step{step['id']:02d}"))
            self.evidence.trace("unknown_dialog", step=step["id"], text=dialog[:200])
            return {
                "kind": "intervention",
                "step": step,
                "screenshot": screenshot,
                "refusal": RiskRefused(
                    "UNEXPECTED_DIALOG",
                    f"An unrecognised dialog appeared after step {step['id']}: "
                    f"{dialog[:160]!r}",
                    "Dismiss or defer it, then resume once the expected page is visible."),
            }

        self.surface.click(element_for(known.dismiss), strategies=known.dismiss["strategies"])
        self._recovered("KNOWN_INTERSTITIAL", step, action="dismissed", name=known.name)
        return None

    def _verify(self, step, inputs, base_url):
        """Confirm the step landed where the recording said it would.

        Only navigate and click carry checkpoints — filling a field does not
        transition the page, and asserting one there would be theatre.

        A checkpoint that does not hold yet is not the same as one that will not.
        A slow page is retried with backoff and the recovery is logged in the trace;
        it never becomes a terminal status of its own (§6). Session expiry is
        checked first, because no amount of waiting fixes being logged out.
        """
        checkpoint = step.get("checkpoint")
        if not checkpoint:
            return None
        expected = resolve(checkpoint["value"], inputs, base_url)
        condition = checkpoint["condition"]
        attempts = self.runtime.slow_load.max_attempts if self.runtime else 1

        # The action's own bounded wait was the first attempt at this page. It
        # expired, so re-checking here is already attempt two — and the recovery
        # has to be counted from there rather than from the loop below, because a
        # page that arrives while the *first* re-check is reading it satisfies the
        # checkpoint immediately and would otherwise look like nothing ever went
        # wrong. A run that silently took thirteen seconds is the failure mode
        # this whole class of logging exists to prevent.
        late = self._timed_out_at == step["id"]

        # Wait before diagnosing. A page that is merely slow is indistinguishable
        # from one that will never arrive: mid-navigation the browser still shows
        # the previous URL, so classifying early reads an 8s stall as an expired
        # session and sends the run through a re-login it did not need. The cost of
        # waiting first is a few seconds on the genuinely-expired path; the cost of
        # not waiting is a confident wrong diagnosis.
        for attempt in range(1, attempts + 1):
            if self._holds(condition, expected):
                waits = attempt + (1 if late else 0)
                if waits > 1:
                    self._recovered("SLOW_LOAD", step, attempts=waits,
                                    # Re-checked, never re-sent: §6's rule made
                                    # visible in the trace rather than only in prose.
                                    action="re-checked" if late else "retried")
                self.evidence.trace("checkpoint", step=step["id"], passed=True,
                                    condition=condition, value=expected)
                return None
            if attempt < attempts:
                time.sleep(self.runtime.slow_load.delay_before(attempt))
                self.surface.settle()

        if self._session_expired():
            if self._recover_session(step, inputs, base_url, condition, expected):
                return None
            screenshot = self.surface.screenshot(
                self.evidence.screenshot_path(f"intervention_step{step['id']:02d}"))
            missing = "" if self.runtime.session.recoverable else \
                " and no re-login routine is defined for this app"
            return {
                "kind": "intervention", "step": step, "screenshot": screenshot,
                "refusal": RiskRefused(
                    "SESSION_EXPIRED",
                    f"The session expired before step {step['id']} could be "
                    f"verified{missing}.",
                    "Sign back in and resume, or abandon the run."),
            }

        return self._failure(step, StepFailure(
            f"checkpoint {condition} {expected!r} after step {step['id']}",
            f"not satisfied after {attempts} attempt(s); page is "
            f"{self.surface.current_url()!r}",
            self.strategy_log[-1:]))

    # ---------------------------------------------------------- recovery --

    def _recovered(self, condition, step, **detail):
        """A recoverable condition is logged and execution continues.

        Deliberately not a status: the caller asked whether the capability
        succeeded, not whether the network was having a bad afternoon. Recording it
        in the trace keeps the information for whoever is debugging without
        promoting it to something a caller has to branch on.
        """
        record = {"condition": condition, "step": step["id"], **detail}
        self.recoveries.append(record)
        self.evidence.trace("recovered", **record)

    def _session_expired(self):
        """Being on the login page is not evidence of anything on its own.

        It is a legitimate destination at the start of every capability, so the
        runtime config declares only how to *recognise* it and the engine supplies
        the context: a session can only have been *lost* if one was ever
        established, and establishing one means credentials were submitted. Without
        that guard, a step that fails its checkpoint anywhere before sign-on gets
        diagnosed as an expired session and sent through a pointless re-login.
        """
        if not self.runtime or not self.runtime.session.detect:
            return False
        if not self._credentials_submitted:
            return False
        return self._matches_any(self.runtime.session.detect,
                                 self.surface.page_text(), self.surface.current_url())

    def _recover_session(self, step, inputs, base_url, condition, expected):
        """Re-authenticate using the app's declared routine, then re-check.

        The routine is executed by this same interpreter, from the same instruction
        set as an artifact — a recovery path with its own machinery would be a
        second, less-tested engine running exactly when things are already wrong.

        Deliberately limited: after signing back in, the step's checkpoint is
        re-verified and the run continues only if the state it expected has been
        restored. Replaying the steps that established that state would be the
        fuller answer, and is not attempted, because re-running a prefix is only
        safe if it contains no mutations *and* the app behaves identically when
        already authenticated — which is not true here, where the root URL redirects
        to the search screen once signed in. Escalating is the honest alternative.
        """
        if not self.runtime.session.recoverable:
            return False

        self.evidence.trace("session_recovery_started", step=step["id"])
        for routine_step in self.runtime.session.relogin:
            self._execute(routine_step, inputs, base_url, event="recovery_step")

        if self._holds(condition, expected):
            self._recovered("SESSION_EXPIRED", step, action="re-authenticated")
            return True
        self.evidence.trace("session_recovery_failed", step=step["id"])
        return False

    def _holds(self, condition, expected):
        if condition == "text_present":
            return self.surface.text_present(expected)
        return expected in self.surface.current_url()

    def _matches_any(self, conditions, text, url):
        for condition in conditions or ():
            if condition["condition"] == "text_present":
                if self.surface.text_present(condition["value"], haystack=text):
                    return condition
            elif condition["value"] in url:
                return condition
        return None

    def _match_outcome(self, text, url):
        """Which declared business outcome, if any, the page is now showing.

        Checked after every step rather than at the step where it is expected:
        runtime surprises do not respect the step you planned for them at, and the
        markers are specific text, so the false-positive risk is low and the cost
        is one page read. That read is shared across every declared outcome.
        """
        for outcome in self.artifact.get("expected_outcomes", []):
            if self._matches_any([outcome["detect"]], text, url):
                return outcome
        return None

    # --------------------------------------------------------- conclusions --

    def _finish(self):
        checkpoint = self.artifact["success"]["checkpoint"]
        if not self._holds(checkpoint["condition"], checkpoint["value"]):
            raise_at = len(self.artifact["steps"])
            return self._failure(
                self.artifact["steps"][raise_at - 1],
                StepFailure(f"success checkpoint {checkpoint['value']!r}",
                            "every step ran but the success condition was not met",
                            self.strategy_log[-1:]))
        return {"kind": "success"}

    def _business_outcome(self, step, outcome):
        """A terminal status the caller branches on — not an error.

        Screenshotted on detection (§10) because this is an answer someone may have
        to justify later, and "the system said the member was out of region" is
        worth being able to show rather than assert.
        """
        screenshot = self.surface.screenshot(
            self.evidence.screenshot_path(f"outcome_step{step['id']:02d}"))
        detail = (f"After step {step['id']} ({step['action']}), the page matched "
                  f"{outcome['detect']['value']!r}. {outcome['meaning']}")
        self.evidence.trace("business_outcome", step=step["id"],
                            outcome_code=outcome["code"], detail=detail)
        return {"kind": "business_outcome", "step": step, "outcome": outcome,
                "detail": detail, "screenshot": screenshot}

    def _intervention(self, step, refusal):
        screenshot = self.surface.screenshot(
            self.evidence.screenshot_path(f"intervention_step{step['id']:02d}"))
        self.evidence.trace("paused_for_human", step=step["id"], reason=refusal.reason,
                            detail=refusal.detail)
        return {"kind": "intervention", "step": step, "refusal": refusal,
                "screenshot": screenshot}

    def _failure(self, step, failure):
        expected = getattr(failure, "expected", "the step to complete")
        observed = getattr(failure, "observed", str(failure))
        tried = getattr(failure, "strategies_tried", None) or self.strategy_log[-1:]

        screenshot = self.surface.screenshot(
            self.evidence.screenshot_path(f"failure_step{step['id']:02d}"))
        dom = self.surface.dom_snapshot(
            self.evidence.dir / f"failure_step{step['id']:02d}.html")
        self.evidence.trace("hard_failure", step=step["id"], expected=expected,
                            observed=observed, strategies_tried=tried)
        return {"kind": "failure", "step": step, "expected": expected,
                "observed": observed, "strategies_tried": tried,
                "screenshot": screenshot, "dom_snapshot": dom}

    # ------------------------------------------------------------ envelope --

    def _envelope(self, outcome, started_at, inputs, fingerprint_seen):
        from ..contracts import validate_result

        capability = self.artifact["capability"]
        recorded = capability["recorded_against"]["app_fingerprint"]

        envelope = {
            "run_id": self.evidence.run_id,
            "capability": capability["name"],
            "capability_version": capability["version"],
            "mode": "replay",
            "inputs": inputs,
            "started_at": started_at,
            "ended_at": utc_now(),
            "steps_completed": self.steps_completed,
            "steps_total": len(self.artifact["steps"]),
            # Not a constant: counted, and the schema refuses anything else here.
            "llm_call_count": 0,
            "evidence_path": self.evidence.path,
        }

        if fingerprint_seen:
            envelope["app_fingerprint_observed"] = fingerprint_seen
            if fingerprint_seen != recorded:
                # A warning, not a failure: most app changes do not touch the flow
                # that was recorded, and the strategy chain is what actually copes.
                envelope["drift_warning"] = (
                    f"recorded against {recorded}, ran against {fingerprint_seen}; "
                    f"locators may have drifted")
                self.evidence.trace("drift", recorded=recorded, observed=fingerprint_seen)

        kind = outcome["kind"]
        if kind == "success":
            envelope.update(status="SUCCESS", payload={
                "outputs": self.outputs, "checkpoint_verified": True})
        elif kind == "business_outcome":
            envelope.update(status="BUSINESS_OUTCOME", payload={
                "outcome_code": outcome["outcome"]["code"],
                "detected_at_step": outcome["step"]["id"],
                "detail": outcome["detail"],
                "evidence": outcome["screenshot"],
            })
        elif kind == "intervention":
            refusal = outcome["refusal"]
            envelope.update(status="NEEDS_INTERVENTION", payload={
                "reason": refusal.reason,
                "detail": refusal.detail,
                "paused_at_step": outcome["step"]["id"],
                "screenshot": outcome["screenshot"],
                "session_id": self.surface.session_id,
                "requested_action": refusal.requested_action,
                "control": "HUMAN",
            })
        else:
            envelope.update(status="HARD_FAILURE", payload={
                "failed_at_step": outcome["step"]["id"],
                "action_attempted": outcome["step"]["action"],
                "expected": outcome["expected"],
                "observed": outcome["observed"],
                "strategies_tried": outcome["strategies_tried"],
                "screenshot": outcome["screenshot"],
                "dom_snapshot": outcome["dom_snapshot"],
            })
        return validate_result(envelope)
