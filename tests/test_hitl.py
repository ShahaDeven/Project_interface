"""Tests for the handoff seam (DESIGN §9) — Day 4a, the state machine.

The console is an interface precisely so these can exist. `ScriptedConsole` takes
an `act` callback that runs while control is HUMAN with the live page in front of
it, so a test can do what an operator would do — dismiss the modal, confirm the
step — and then answer. That is what makes the resume path, the part most likely
to be wrong, the part that gets tested most.

Not here yet (4b): capture of human actions as `human_action` trace entries, and
the `intervention_record` on the final envelope.
"""

import io
import threading
from pathlib import Path

import pytest

from cua.contracts import ContractError, load_artifact, validate_result
from cua.evidence import RunEvidence
from cua.executor.browser import BANNER_MARKER
from cua.hitl import (ABANDON, AUTOMATION, HUMAN, RESUME, ControlViolation,
                      Handoff, InterventionRequest, ScriptedConsole, TerminalConsole)
from cua.replay import ReplayEngine, RuntimeConfig

CAPABILITIES = Path(__file__).resolve().parents[1] / "capabilities"

SUB_ACCOUNT_PARAMS = {
    "member_number": "23456", "account_type": "Holiday Club",
    "account_nickname": "Vacation fund", "initial_deposit": "150.00",
}


@pytest.fixture
def lookup():
    return load_artifact(CAPABILITIES / "lookup_member_balance.json")


@pytest.fixture
def sub_account():
    return load_artifact(CAPABILITIES / "open_sub_account.json")


def request_for(**overrides):
    fields = dict(run_id="run_x", capability="open_sub_account", step=12,
                  steps_total=13, reason="IRREVERSIBLE_STEP", detail="because",
                  requested_action="Confirm or abandon.")
    fields.update(overrides)
    return InterventionRequest(**fields)


# =============================================================================
# The console — no browser needed
# =============================================================================

class TestTerminalConsole:

    def console(self, answers, tty=True, deadline=5):
        replies = iter(answers)
        stdin = type("Stdin", (), {"isatty": lambda self: tty})()
        console = TerminalConsole(stream=io.StringIO(),
                                  input_fn=lambda _prompt: next(replies),
                                  stdin=stdin)
        # Production waits forever for a person. A test must not: a channel that
        # never delivers would hang the suite instead of failing it.
        console.deadline_seconds = deadline
        return console

    def test_a_pipe_is_not_an_operator(self):
        """Without this a run in CI hits input(), takes an immediate EOF, and
        reports itself abandoned by someone who was never there."""
        assert self.console([], tty=False).available() is False
        assert self.console([], tty=True).available() is True

    def test_the_request_is_rendered_before_the_prompt(self):
        console = self.console(["r"])
        console.ask(request_for(screenshot="evidence/run_x/s.png"))
        shown = console.stream.getvalue()
        assert "YOUR ACTION IS NEEDED" in shown
        assert "IRREVERSIBLE_STEP" in shown
        assert "step 12 of 13" in shown
        assert "evidence/run_x/s.png" in shown
        assert "\a" in shown, "the bell is the one real interrupt available"
        assert "Do NOT close it" in shown

    def test_a_gate_tells_the_operator_not_to_do_it_themselves(self):
        """The defect this replaced: 'Confirm this specific action' reads, to a
        person standing at a live browser, as 'go and click it' — and then the
        automation resumes into a page where its target is gone."""
        console = self.console(["r"])
        console.ask(request_for(when="before_step"))
        shown = console.stream.getvalue()
        assert "has NOT run yet" in shown
        assert "Do NOT perform it yourself" in shown
        assert "approve and run this step" in shown

    def test_a_pause_after_the_action_invites_them_to_act(self):
        console = self.console(["r"])
        console.ask(request_for(when="after_step"))
        shown = console.stream.getvalue()
        assert "the page it landed on" in shown
        assert "Do what is needed in the browser" in shown

    def test_the_banner_carries_the_same_distinction(self):
        """It is the channel the operator is actually looking at, so it cannot be
        the one that gives the wrong instruction."""
        gate = " ".join(request_for(when="before_step").banner_lines())
        after = " ".join(request_for(when="after_step").banner_lines())
        assert "Do NOT do this yourself" in gate
        assert "Do what is needed here" in after

    @pytest.mark.parametrize("answer,expected", [
        ("r", RESUME), ("resume", RESUME), ("a", ABANDON), ("abandon", ABANDON),
    ])
    def test_answers_are_understood(self, answer, expected):
        assert self.console([answer]).ask(request_for()) == expected

    def test_an_unreadable_answer_is_asked_again(self):
        console = self.console(["what?", "r"])
        assert console.ask(request_for()) == RESUME
        assert "Answer 'r' to resume" in console.stream.getvalue()

    def test_a_decision_made_on_the_page_is_taken(self):
        """The operator is in the browser. Making them switch windows to answer is
        how the first human run ended up clicking the application's own button."""
        console = self.console([])
        assert console.ask(request_for(), watch=lambda: RESUME) == RESUME
        assert "chosen on the page" in console.stream.getvalue()

    def test_the_keyboard_still_works_when_the_page_offers_buttons(self):
        """Both channels, first answer wins — the page may be unusable, which is
        occasionally the very reason someone was called."""
        console = self.console(["a"])
        assert console.ask(request_for(), watch=lambda: None) == ABANDON

    def test_a_deadline_ends_the_wait_rather_than_hanging(self):
        """Production waits indefinitely — there is no defensible number of seconds
        after which an operator's answer stops mattering, least of all on the
        irreversible step. A bounded wait exists so that a channel which never
        delivers fails loudly instead of hanging whoever is running this.

        The keyboard has to *block* here, the way a present-but-silent operator
        does. An input that raises instead takes the closed-stdin path below, which
        also returns ABANDON and would let this pass while proving nothing.
        """
        silent = threading.Event()
        stdin = type("Stdin", (), {"isatty": lambda self: True})()
        console = TerminalConsole(stream=io.StringIO(),
                                  input_fn=lambda _prompt: silent.wait(),
                                  stdin=stdin)
        console.deadline_seconds = 0.5

        assert console.ask(request_for(), watch=lambda: None) == ABANDON
        assert "nobody answered" in console.stream.getvalue()

    def test_stdin_closing_mid_poll_is_also_abandon(self):
        """Nobody left to ask is a decision too, and a different one from nobody
        answering — the reader reports the channel gone rather than staying silent."""
        console = self.console([], deadline=30)
        assert console.ask(request_for(), watch=lambda: None) == ABANDON
        assert "abandoned" in console.stream.getvalue()

    def test_waiting_forever_is_the_default(self):
        assert TerminalConsole.deadline_seconds is None

    def test_the_prompt_says_both_channels_are_open(self):
        console = self.console([])
        console.ask(request_for(), watch=lambda: RESUME)
        assert "buttons on the page" in console.stream.getvalue()

    def test_the_banner_offers_the_right_two_choices(self):
        gate = dict(request_for(when="before_step").banner_choices())
        after = dict(request_for(when="after_step").banner_choices())
        assert gate[RESUME] == "Approve and run this step"
        assert after[RESUME] == "Done — resume"
        assert gate[ABANDON] == after[ABANDON] == "Abandon run"

    def test_an_interrupt_at_the_prompt_is_a_decision_not_a_crash(self):
        def interrupt(_prompt):
            raise KeyboardInterrupt
        stdin = type("Stdin", (), {"isatty": lambda self: True})()
        console = TerminalConsole(stream=io.StringIO(), input_fn=interrupt, stdin=stdin)
        assert console.ask(request_for()) == ABANDON


class TestHandoffStateMachine:
    """Control is real state with a guard behind it, not an implication of the
    call order — the human and the automation on the same page at once is how two
    clicks land on one confirmation button."""

    def handoff(self, tmp_path, console=None, run_id="run_hitl"):
        evidence = RunEvidence(run_id=run_id, root=tmp_path)
        return Handoff(surface=None, evidence=evidence, console=console), evidence

    def test_control_starts_with_the_automation(self, tmp_path):
        handoff, _ = self.handoff(tmp_path)
        assert handoff.control == AUTOMATION
        handoff.check_may_act("click")          # does not raise

    def test_acting_while_a_human_holds_control_is_refused(self, tmp_path):
        handoff, _ = self.handoff(tmp_path)
        handoff.control = HUMAN
        with pytest.raises(ControlViolation, match="one writer at a time"):
            handoff.check_may_act("click")

    def test_control_is_returned_even_if_the_console_throws(self, tmp_path):
        """A console that dies must not leave the run believing a human is still
        driving; the automation would then refuse to act for the rest of the run."""
        class Broken:
            def available(self): return True
            def ask(self, _request, watch=None): raise RuntimeError("console died")

        handoff, _ = self.handoff(tmp_path, Broken())
        with pytest.raises(RuntimeError):
            handoff.offer(request_for())
        assert handoff.control == AUTOMATION

    def test_no_console_means_the_pause_is_terminal(self, tmp_path):
        """An unattended run does not get to approve its own mutations."""
        handoff, evidence = self.handoff(tmp_path)
        assert handoff.offer(request_for()) == ABANDON
        assert "no_operator_console" in [e["event"] for e in evidence.read_trace()]

    def test_both_transitions_are_traced(self, tmp_path):
        handoff, evidence = self.handoff(tmp_path, ScriptedConsole(RESUME))
        handoff.offer(request_for())
        events = {e["event"]: e for e in evidence.read_trace()}
        assert events["control_transferred"]["to"] == HUMAN
        assert events["control_returned"]["to"] == AUTOMATION
        assert events["control_returned"]["decision"] == RESUME
        assert events["control_returned"]["paused_at"]

    def test_repeated_pauses_on_one_step_are_bounded(self, tmp_path):
        """An operator can answer 'resume' without having changed anything. The
        engine re-verifies and pauses again, correctly and forever — so a third
        identical request is information about the situation, not the page."""
        handoff, evidence = self.handoff(tmp_path, ScriptedConsole(RESUME))
        decisions = [handoff.offer(request_for()) for _ in range(5)]
        assert decisions == [RESUME] * Handoff.max_pauses_per_step + [ABANDON, ABANDON]
        assert "handoff_exhausted" in [e["event"] for e in evidence.read_trace()]

    def test_the_bound_is_per_step_not_per_run(self, tmp_path):
        handoff, _ = self.handoff(tmp_path, ScriptedConsole(RESUME))
        for _ in range(Handoff.max_pauses_per_step):
            handoff.offer(request_for(step=4))
        assert handoff.offer(request_for(step=5)) == RESUME

    def test_what_the_record_will_be_built_from_is_kept(self, tmp_path):
        """4b's intervention_record is assembled from these."""
        handoff, _ = self.handoff(tmp_path, ScriptedConsole(ABANDON))
        handoff.offer(request_for(step=9, reason="UNEXPECTED_DIALOG"))
        assert handoff.pauses[0]["step"] == 9
        assert handoff.pauses[0]["decision"] == ABANDON
        assert handoff.pauses[0]["paused_at"] <= handoff.pauses[0]["control_returned_at"]


# =============================================================================
# Channel 1 — the banner on the live page
# =============================================================================

@pytest.mark.browser
class TestBanner:

    def test_the_banner_is_painted_onto_the_page(self, surface, live_server):
        surface.navigate(f"{live_server}/login")
        surface.show_banner("PAUSED", ["step 4", "do the thing"], title="[!] ACTION")
        assert surface.text_present("do the thing")
        assert surface.page.title() == "[!] ACTION"

    def test_the_banner_survives_a_navigation(self, surface, live_server):
        """A paused run is exactly when the operator is navigating, and a notice
        that dies on the first click is a notice nobody sees."""
        surface.navigate(f"{live_server}/login")
        surface.show_banner("PAUSED", ["still here"])
        surface.navigate(f"{live_server}/login")
        assert surface.text_present("still here")

    def test_clearing_restores_the_page_title(self, surface, live_server):
        surface.navigate(f"{live_server}/login")
        before = surface.page.title()
        surface.show_banner("PAUSED", [], title="[!] ACTION")
        surface.clear_banner()
        assert surface.page.title() == before
        assert not surface.page.query_selector(f"[{BANNER_MARKER}]")

    def test_the_banner_is_not_mistaken_for_a_modal(self, surface, live_server):
        """It is fixed-position furniture, which is exactly how runtime.yaml
        recognises a modal on this app. Unexcluded, the engine pauses a second
        time over a notice it painted itself."""
        selector = RuntimeConfig.load("legacy-cu-portal").dialogs.detect_selector
        surface.navigate(f"{live_server}/login")
        surface.show_banner("PAUSED", ["not a dialog"])
        assert surface.dialog_present(selector) is None

    def test_the_buttons_report_what_was_pressed(self, surface, live_server):
        """Channel 1 becomes the answering channel too, not only the notice."""
        surface.navigate(f"{live_server}/login")
        surface.show_banner("PAUSED", ["choose"],
                            choices=((RESUME, "Approve"), (ABANDON, "Abandon run")))
        assert surface.banner_decision() is None

        surface.page.click('button:has-text("Approve")')
        assert surface.banner_decision() == RESUME

    def test_the_poll_restores_a_banner_that_went_missing(self, surface, live_server):
        """During a handoff the engine navigates nothing — the *operator* does — so
        the decision poll is the only moment anything of ours looks at the page.
        Restoring the notice has to happen there, not in navigate(), and not only in
        a load handler that calls Playwright from inside Playwright's own event
        dispatch. Simulated by deleting the banner outright."""
        surface.navigate(f"{live_server}/login")
        surface.show_banner("PAUSED", ["still needed"], choices=((RESUME, "Approve"),))

        surface.page.evaluate(
            "() => document.querySelectorAll('[data-cua-handoff]').forEach(n => n.remove())")
        assert not surface.page.query_selector(f"[{BANNER_MARKER}]")

        assert surface.banner_decision() is None, "nothing was pressed"
        assert surface.page.query_selector(f"[{BANNER_MARKER}]"), "the poll repainted it"
        assert surface.text_present("still needed")

    def test_a_cleared_banner_is_not_repainted_by_the_poll(self, surface, live_server):
        """The repaint is driven by the handoff being live, not by the page lacking
        a banner. Once control is back, polling must not resurrect it."""
        surface.navigate(f"{live_server}/login")
        surface.show_banner("PAUSED", ["gone soon"])
        surface.clear_banner()
        surface.banner_decision()
        assert not surface.page.query_selector(f"[{BANNER_MARKER}]")

    def test_a_stale_decision_cannot_leak_into_the_next_pause(self, surface, live_server):
        """A run pauses more than once. An answer left on the page would resume
        the next handoff before anyone had seen it."""
        surface.navigate(f"{live_server}/login")
        surface.show_banner("PAUSED", [], choices=((RESUME, "Approve"),))
        surface.page.click('button:has-text("Approve")')
        surface.clear_banner()
        assert surface.banner_decision() is None

    def test_a_real_modal_is_still_detected_underneath_it(self, surface, live_server):
        """The exclusion must be narrow: skip our own furniture, not the app's."""
        # Armed and fired on /login, which renders for an anonymous browser. Any
        # authenticated route would redirect first, and the redirect is a page
        # load that consumes the flag before the test ever sees it.
        selector = RuntimeConfig.load("legacy-cu-portal").dialogs.detect_selector
        surface.navigate(f"{live_server}/login?chaos=dialog")
        surface.navigate(f"{live_server}/login")
        surface.show_banner("PAUSED", ["ours"])
        found = surface.dialog_present(selector)
        assert found and "Scheduled maintenance" in found
        assert "ours" not in found


# =============================================================================
# The engine, resuming — against the live app
# =============================================================================

class Harness:
    """Shared setup. Not named Test* on purpose — a collected base class runs its
    every test again for each subclass, which here would mean a second pass of
    the browser suite."""

    @pytest.fixture(autouse=True)
    def _credentials(self, monkeypatch):
        monkeypatch.setenv("CUA_SECRET_OPERATOR_ID", "e.okafor")
        monkeypatch.setenv("CUA_SECRET_OPERATOR_PASSWORD", "irrelevant")

    def engine(self, surface, artifact, tmp_path, run_id, **kwargs):
        evidence = RunEvidence(run_id=run_id, root=tmp_path)
        return ReplayEngine(surface, artifact, evidence=evidence,
                            runtime=RuntimeConfig.load("legacy-cu-portal"),
                            **kwargs), evidence


@pytest.mark.browser
class TestResume(Harness):

    def test_an_approved_irreversible_step_completes_the_mutation(
            self, surface, sub_account, live_server, tmp_path):
        """4a's headline. This capability has never finished a replay: step 12 is
        irreversible and always stops, so steps 12 and 13 were unexecuted code and
        `new_account_number` had only ever been produced by the LLM at discovery.
        A human confirming is the only thing that can complete it."""
        from target_app import data

        console = ScriptedConsole(RESUME)
        engine, evidence = self.engine(surface, sub_account, tmp_path, "run_4a_ok",
                                       approve_mutations=True, console=console)
        result = engine.run(SUB_ACCOUNT_PARAMS, live_server)

        assert result.status == "SUCCESS"
        assert result.outputs["new_account_number"]
        assert result.envelope["steps_completed"] == 13

        opened = data.sub_accounts_for("23456")
        assert len(opened) == 1, "the mutation has to be real, not mimed"

        assert [r.reason for r in console.requests] == ["IRREVERSIBLE_STEP"]
        assert console.requests[0].when == "before_step", "the step had not run yet"

    def test_the_confirmation_is_submitted_exactly_once(
            self, surface, sub_account, live_server, tmp_path):
        """A pause before the action resumes by running it; a pause after resumes
        by re-judging the page. Confusing the two is how a confirmation double-posts."""
        console = ScriptedConsole(RESUME)
        engine, evidence = self.engine(surface, sub_account, tmp_path, "run_4a_once",
                                       approve_mutations=True, console=console)
        engine.run(SUB_ACCOUNT_PARAMS, live_server)

        executed = [e["step"] for e in evidence.read_trace() if e["event"] == "step"]
        assert executed == sorted(set(executed)), "a step ran twice"
        assert executed == list(range(1, 14))

        from target_app import data
        assert len(data.sub_accounts_for("23456")) == 1

    def test_declining_leaves_the_run_exactly_as_it_was(
            self, surface, sub_account, live_server, tmp_path):
        """Abandon has to be indistinguishable from the unattended path, or the
        gate becomes a prompt people learn to click through."""
        from target_app import data

        engine, _ = self.engine(surface, sub_account, tmp_path, "run_4a_no",
                                approve_mutations=True,
                                console=ScriptedConsole(ABANDON))
        result = engine.run(SUB_ACCOUNT_PARAMS, live_server)

        assert result.status == "NEEDS_INTERVENTION"
        assert result.envelope["payload"]["reason"] == "IRREVERSIBLE_STEP"
        assert result.envelope["steps_completed"] == 11
        assert data.sub_accounts_for("23456") == []

    def test_both_gates_are_offered_in_turn(
            self, surface, sub_account, live_server, tmp_path):
        """Without --approve-mutations there are two pauses, and the standing
        approval never covers the irreversible one."""
        console = ScriptedConsole(RESUME)
        engine, _ = self.engine(surface, sub_account, tmp_path, "run_4a_both",
                                console=console)
        result = engine.run(SUB_ACCOUNT_PARAMS, live_server)

        assert result.status == "SUCCESS"
        assert [r.reason for r in console.requests] == [
            "MUTATION_NOT_APPROVED", "IRREVERSIBLE_STEP"]

    def test_an_operator_who_performs_the_step_themselves_is_not_double_posted(
            self, surface, sub_account, live_server, tmp_path):
        """A gate asks for permission, but a person standing at a live browser may
        press the button anyway. The automation must not press it a second time —
        this is the irreversible step, so re-sending is a second sub-account on a
        member's record."""
        from target_app import data

        def confirm_it_myself(_request):
            surface.page.click('input[value="Confirm and Open Account"]')

        engine, evidence = self.engine(
            surface, sub_account, tmp_path, "run_4c_human",
            approve_mutations=True,
            console=ScriptedConsole(RESUME, act=confirm_it_myself))
        result = engine.run(SUB_ACCOUNT_PARAMS, live_server)

        assert result.status == "SUCCESS"
        assert len(data.sub_accounts_for("23456")) == 1, "the step was re-sent"
        assert result.outputs["new_account_number"]

        events = [e["event"] for e in evidence.read_trace()]
        assert "performed_by_human" in events
        assert "hard_failure" not in events

    def test_a_vanished_target_is_still_a_failure_when_nothing_happened(
            self, surface, sub_account, live_server, tmp_path):
        """The tolerance is narrow on purpose: the step's own checkpoint has to
        hold. Without that, 'already done' and 'never happened' are the same
        observation, and guessing between them on an irreversible step is not a
        trade worth making."""
        def wander_off(_request):
            surface.navigate(f"{live_server}/search")

        engine, _ = self.engine(
            surface, sub_account, tmp_path, "run_4c_gone",
            approve_mutations=True,
            console=ScriptedConsole(RESUME, act=wander_off))
        result = engine.run(SUB_ACCOUNT_PARAMS, live_server)

        assert result.status == "HARD_FAILURE"
        from target_app import data
        assert data.sub_accounts_for("23456") == []

    def test_an_operator_who_acts_lets_the_run_continue(
            self, surface, lookup, live_server, tmp_path):
        """The §10 demo #5 shape, rehearsed without a person: an unknown dialog
        stops the run, the operator dismisses it in the live browser, and the run
        picks up from the page it was left on."""
        def acknowledge(_request):
            surface.page.click('input[value="Acknowledge"]')

        console = ScriptedConsole(RESUME, act=acknowledge)
        engine, evidence = self.engine(surface, lookup, tmp_path, "run_4a_dialog",
                                       chaos="dialog", console=console)
        result = engine.run({"member_number": "23456"}, live_server)

        assert result.status == "SUCCESS"
        assert result.outputs == {"savings_balance": 18240.55}
        assert [r.reason for r in console.requests] == ["UNEXPECTED_DIALOG"]

        trace = evidence.read_trace()
        events = [e["event"] for e in trace]
        assert "paused_for_human" in events and "resuming" in events
        paused = next(e for e in trace if e["event"] == "paused_for_human")
        assert paused["when"] == "after_step", "the step ran; resuming re-judges it"

    def test_resuming_without_acting_never_blind_continues(
            self, surface, lookup, live_server, tmp_path):
        """The operator says resume and has changed nothing. The engine re-judges
        the page rather than taking their word for it, gets the same answer, and
        stops after a bounded number of rounds."""
        console = ScriptedConsole(RESUME)
        engine, evidence = self.engine(surface, lookup, tmp_path, "run_4a_blind",
                                       chaos="dialog", console=console)
        result = engine.run({"member_number": "23456"}, live_server)

        assert result.status == "NEEDS_INTERVENTION"
        assert result.envelope["payload"]["reason"] == "UNEXPECTED_DIALOG"
        assert len(console.requests) == Handoff.max_pauses_per_step
        assert "handoff_exhausted" in [e["event"] for e in evidence.read_trace()]

    def test_without_a_console_nothing_changes(
            self, surface, sub_account, live_server, tmp_path):
        """The whole of 4a is opt-in: an engine with no console behaves exactly as
        it did before the handoff existed."""
        engine, evidence = self.engine(surface, sub_account, tmp_path, "run_4a_none",
                                       approve_mutations=True)
        result = engine.run(SUB_ACCOUNT_PARAMS, live_server)

        assert result.status == "NEEDS_INTERVENTION"
        assert result.envelope["steps_completed"] == 11
        assert "control_transferred" not in [e["event"] for e in evidence.read_trace()]
        assert "intervention_record" not in result.envelope, \
            "a run that never paused has nothing to record"


# =============================================================================
# The intervention record — the audit trail over the manual segment (§7)
# =============================================================================

@pytest.mark.browser
class TestInterventionRecord(Harness):

    def only(self, result):
        record = result.envelope["intervention_record"]
        assert len(record["interventions"]) == 1
        return record, record["interventions"][0]

    def test_an_approved_handoff_is_recorded_against_the_result(
            self, surface, sub_account, live_server, tmp_path):
        """The run succeeded, and the record is how anyone later learns a human
        was in the middle of it."""
        engine, _ = self.engine(surface, sub_account, tmp_path, "run_4b_ok",
                                approve_mutations=True, console=ScriptedConsole(RESUME))
        result = engine.run(SUB_ACCOUNT_PARAMS, live_server)

        assert result.status == "SUCCESS"
        assert validate_result(result.envelope)

        record, entry = self.only(result)
        assert record["operator"] == "scripted"
        assert entry["paused_at_step"] == 12
        assert entry["reason"] == "IRREVERSIBLE_STEP"
        assert entry["when"] == "before_step"
        assert entry["decision"] == "resume"
        assert entry["resolution"] == "verified"
        assert entry["paused_at"] <= entry["control_returned_at"]

    def test_the_post_resume_checkpoint_is_the_one_that_was_re_verified(
            self, surface, sub_account, live_server, tmp_path):
        """Never blind-resume, stated in the record rather than only in the code:
        step 12 is confirmed to have landed where the recording said it would."""
        engine, _ = self.engine(surface, sub_account, tmp_path, "run_4b_cp",
                                approve_mutations=True, console=ScriptedConsole(RESUME))
        _, entry = self.only(engine.run(SUB_ACCOUNT_PARAMS, live_server))

        checkpoint = entry["post_resume_checkpoint"]
        assert checkpoint["passed"] is True
        assert checkpoint["condition"] == "url_matches"
        assert "/sub-account/confirm" in checkpoint["value"]

    def test_every_handoff_is_kept_not_just_the_last(
            self, surface, sub_account, live_server, tmp_path):
        """Two gates, two entries. Reporting one would drop the record of a human
        approving a write to a member's account, which is the entry an audit is
        for."""
        engine, _ = self.engine(surface, sub_account, tmp_path, "run_4b_two",
                                console=ScriptedConsole(RESUME))
        result = engine.run(SUB_ACCOUNT_PARAMS, live_server)

        entries = result.envelope["intervention_record"]["interventions"]
        assert [(e["paused_at_step"], e["reason"]) for e in entries] == [
            (11, "MUTATION_NOT_APPROVED"), (12, "IRREVERSIBLE_STEP")]
        assert all(e["resolution"] == "verified" for e in entries)

    def test_a_declined_handoff_is_recorded_too(
            self, surface, sub_account, live_server, tmp_path):
        """The record covers refusals: 'someone was asked and said no' is a
        different fact from 'nobody was asked', and both end the run."""
        engine, _ = self.engine(surface, sub_account, tmp_path, "run_4b_no",
                                approve_mutations=True,
                                console=ScriptedConsole(ABANDON))
        result = engine.run(SUB_ACCOUNT_PARAMS, live_server)

        assert result.status == "NEEDS_INTERVENTION"
        assert validate_result(result.envelope)

        _, entry = self.only(result)
        assert entry["decision"] == "abandon"
        assert entry["resolution"] == "not_resumed"
        assert "post_resume_checkpoint" not in entry, "nothing was resumed into"

    def test_what_the_operator_did_is_captured_not_reported(
            self, surface, lookup, live_server, tmp_path):
        """Captured from the page, not from what the human says they did."""
        def acknowledge(_request):
            surface.page.click('input[value="Acknowledge"]')

        engine, evidence = self.engine(surface, lookup, tmp_path, "run_4b_act",
                                       chaos="dialog",
                                       console=ScriptedConsole(RESUME, act=acknowledge))
        result = engine.run({"member_number": "23456"}, live_server)

        assert result.status == "SUCCESS"
        _, entry = self.only(result)
        action = entry["human_actions"][0]
        assert action["summary"] != "no observable change"
        assert action["url"]

        captured = next(e for e in evidence.read_trace() if e["event"] == "human_action")
        assert captured["changed"] is True
        assert Path(captured["dom_before"]).exists()
        assert Path(captured["dom_after"]).exists()

    def test_doing_nothing_is_recorded_as_doing_nothing(
            self, surface, lookup, live_server, tmp_path):
        """The blind-resume case, in the audit trail: the operator answered, the
        page had not moved, and the engine refused to take their word for it."""
        engine, _ = self.engine(surface, lookup, tmp_path, "run_4b_nothing",
                                chaos="dialog", console=ScriptedConsole(RESUME))
        result = engine.run({"member_number": "23456"}, live_server)

        entries = result.envelope["intervention_record"]["interventions"]
        assert len(entries) == Handoff.max_pauses_per_step
        assert all(e["decision"] == "resume" for e in entries)
        assert all(e["resolution"] == "paused_again" for e in entries)
        assert all(e["human_actions"][0]["summary"] == "no observable change"
                   for e in entries)

    def test_the_captured_page_is_the_one_the_operator_saw(
            self, surface, sub_account, live_server, tmp_path):
        """The before-snapshot is taken before the banner goes up, so evidence
        shows the application rather than the engine's own furniture."""
        engine, evidence = self.engine(surface, sub_account, tmp_path, "run_4b_clean",
                                       approve_mutations=True,
                                       console=ScriptedConsole(RESUME))
        engine.run(SUB_ACCOUNT_PARAMS, live_server)

        captured = next(e for e in evidence.read_trace() if e["event"] == "human_action")
        for snapshot in (captured["dom_before"], captured["dom_after"]):
            assert BANNER_MARKER not in Path(snapshot).read_text(encoding="utf-8")


# =============================================================================
# The contract, without a browser
# =============================================================================

class TestRecordContract:

    def envelope(self, record):
        return {
            "run_id": "run_x", "capability": "open_sub_account",
            "capability_version": "1.0.0", "mode": "replay", "status": "SUCCESS",
            "inputs": {}, "started_at": "2026-08-15T10:00:00Z",
            "ended_at": "2026-08-15T10:01:00Z", "steps_completed": 13,
            "steps_total": 13, "llm_call_count": 0, "evidence_path": "evidence/run_x/",
            "payload": {"outputs": {}, "checkpoint_verified": True},
            "intervention_record": record,
        }

    def entry(self, **overrides):
        fields = {
            "paused_at_step": 12, "reason": "IRREVERSIBLE_STEP",
            "when": "before_step", "requested_action": "Confirm or abandon.",
            "paused_at": "2026-08-15T10:00:30Z",
            "control_returned_at": "2026-08-15T10:00:45Z",
            "decision": "resume", "resolution": "verified",
            "human_actions": [],
        }
        fields.update(overrides)
        return fields

    def test_a_well_formed_record_is_accepted(self):
        assert validate_result(self.envelope({"interventions": [self.entry()]}))

    def test_a_record_with_no_interventions_is_refused(self):
        """An empty record would say a run paused and decline to say when."""
        with pytest.raises(ContractError):
            validate_result(self.envelope({"interventions": []}))

    def test_an_unknown_resolution_is_refused(self):
        """The enum is the point: 'how did this handoff end' has a fixed set of
        answers, and a free-text field would quietly grow prose."""
        with pytest.raises(ContractError):
            validate_result(self.envelope(
                {"interventions": [self.entry(resolution="fine, probably")]}))

    def test_a_pause_that_does_not_say_when_it_sat_is_refused(self):
        """before_step and after_step resume differently; a record that omits it
        cannot be audited for whether the right thing happened."""
        entry = self.entry()
        del entry["when"]
        with pytest.raises(ContractError):
            validate_result(self.envelope({"interventions": [entry]}))
