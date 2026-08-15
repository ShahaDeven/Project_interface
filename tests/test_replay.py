"""Tests for the replay interpreter (DESIGN §6, §7) — Day 3a, the happy path.

These execute the committed artifacts against the real target app in a real
browser, with no model anywhere. That is the point: the entire production path is
verifiable without an API key, and `llm_call_count: 0` is a measured property of a
run rather than a constant someone typed.

Business-outcome scanning and runtime-condition recovery arrive in 3b/3c.
"""

import json
from pathlib import Path

import pytest

from cua.contracts import load_artifact, validate_result
from cua.replay import InputError, ReplayEngine, RuntimeConfig, bind_inputs, parse_params
from cua.replay.binding import ParseError, parse_value, resolve
from cua.evidence import RunEvidence

CAPABILITIES = Path(__file__).resolve().parents[1] / "capabilities"


@pytest.fixture
def lookup():
    return load_artifact(CAPABILITIES / "lookup_member_balance.json")


@pytest.fixture
def sub_account():
    return load_artifact(CAPABILITIES / "open_sub_account.json")


# =============================================================================
# Binding — everything that can fail before a browser opens
# =============================================================================

class TestBinding:

    def test_a_valid_parameter_binds(self, lookup):
        assert bind_inputs(lookup, {"member_number": "23456"}) == {"member_number": "23456"}

    def test_a_missing_required_parameter_is_refused(self, lookup):
        with pytest.raises(InputError, match="required"):
            bind_inputs(lookup, {})

    def test_a_value_failing_the_recorded_pattern_is_refused(self, lookup):
        """The pattern was inferred from the recording; it fails closed, cheaply,
        instead of half-completing a flow."""
        with pytest.raises(InputError, match="does not match"):
            bind_inputs(lookup, {"member_number": "abc"})

    def test_an_unknown_parameter_is_refused(self, lookup):
        """Silently ignoring it would let a caller believe they had set something."""
        with pytest.raises(InputError, match="unknown parameter"):
            bind_inputs(lookup, {"member_number": "23456", "branch": "E14"})

    def test_every_problem_is_reported_at_once(self, sub_account):
        with pytest.raises(InputError) as raised:
            bind_inputs(sub_account, {"member_number": "abc", "nope": "1"})
        assert len(raised.value.problems) >= 2

    def test_param_pairs_keep_equals_signs_in_the_value(self):
        assert parse_params(["a=b=c"]) == {"a": "b=c"}

    def test_a_malformed_pair_is_refused(self):
        with pytest.raises(InputError, match="name=value"):
            parse_params(["member_number"])


class TestTemplating:

    def test_base_url_and_inputs_resolve(self):
        assert resolve("{base_url}/member/{inputs.member_number}",
                       {"member_number": "23456"}, "http://x:5000/") == \
            "http://x:5000/member/23456"

    def test_a_referenced_but_unsupplied_input_is_refused(self):
        with pytest.raises(InputError, match="not supplied"):
            resolve("{inputs.missing}", {}, "http://x")

    def test_secrets_resolve_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("CUA_SECRET_OPERATOR_ID", "e.okafor")
        assert resolve("{secrets.operator_id}", {}, "http://x") == "e.okafor"


class TestParsing:

    @pytest.mark.parametrize("raw,mode,expected", [
        ("$4,523.18", "currency", 4523.18),
        ("$18,240.55", "currency", 18240.55),
        ("712", "integer", 712),
        ("SA-12345-69", "text", "SA-12345-69"),
    ])
    def test_values_are_coerced_to_their_declared_type(self, raw, mode, expected):
        assert parse_value(raw, mode) == expected

    def test_an_unparseable_value_is_an_error_not_a_zero(self):
        """Returning 0.0 for an unreadable balance would be a wrong answer, which
        is worse than a failure."""
        with pytest.raises(ParseError):
            parse_value("not a number", "currency")


# =============================================================================
# The interpreter, against the live app
# =============================================================================

@pytest.mark.browser
class TestReplay:

    params = {"member_number": "23456"}

    def replay(self, surface, artifact, live_server, tmp_path, params=None, **kwargs):
        evidence = RunEvidence(run_id="run_20260814_000001", root=tmp_path)
        engine = ReplayEngine(surface, artifact, evidence=evidence,
                              runtime=RuntimeConfig.load("legacy-cu-portal"), **kwargs)
        return engine.run(params or self.params, live_server), evidence

    def test_a_recorded_flow_replays_for_a_different_member(
            self, surface, lookup, live_server, tmp_path, monkeypatch):
        """The headline claim: recorded against 12345, run against 23456."""
        monkeypatch.setenv("CUA_SECRET_OPERATOR_ID", "e.okafor")
        monkeypatch.setenv("CUA_SECRET_OPERATOR_PASSWORD", "irrelevant")

        result, evidence = self.replay(surface, lookup, live_server, tmp_path)

        assert result.status == "SUCCESS"
        assert result.outputs == {"savings_balance": 18240.55}
        assert isinstance(result.outputs["savings_balance"], float)
        assert validate_result(result.envelope)

    def test_no_model_is_involved(self, surface, lookup, live_server, tmp_path, monkeypatch):
        """Measured, not asserted — and the schema refuses any other value when
        mode is replay."""
        monkeypatch.setenv("CUA_SECRET_OPERATOR_ID", "e.okafor")
        monkeypatch.setenv("CUA_SECRET_OPERATOR_PASSWORD", "irrelevant")

        result, _ = self.replay(surface, lookup, live_server, tmp_path)
        assert result.envelope["mode"] == "replay"
        assert result.envelope["llm_call_count"] == 0

    def test_the_winning_strategy_is_logged_per_step(
            self, surface, lookup, live_server, tmp_path, monkeypatch):
        """Drift telemetry for free: which tier of the chain is carrying the weight."""
        monkeypatch.setenv("CUA_SECRET_OPERATOR_ID", "e.okafor")
        monkeypatch.setenv("CUA_SECRET_OPERATOR_PASSWORD", "irrelevant")

        _, evidence = self.replay(surface, lookup, live_server, tmp_path)
        used = [e["strategy_used"] for e in evidence.read_trace()
                if e["event"] == "step" and e.get("strategy_used")]
        assert used, "no strategy was recorded"
        assert all(entry.startswith("label:") for entry in used), used

    def test_checkpoints_are_verified_and_recorded(
            self, surface, lookup, live_server, tmp_path, monkeypatch):
        monkeypatch.setenv("CUA_SECRET_OPERATOR_ID", "e.okafor")
        monkeypatch.setenv("CUA_SECRET_OPERATOR_PASSWORD", "irrelevant")

        _, evidence = self.replay(surface, lookup, live_server, tmp_path)
        passed = [e for e in evidence.read_trace() if e["event"] == "checkpoint"]
        assert len(passed) == 3
        assert all(e["passed"] for e in passed)

    def test_a_credential_never_reaches_the_evidence(
            self, surface, lookup, live_server, tmp_path, monkeypatch):
        monkeypatch.setenv("CUA_SECRET_OPERATOR_ID", "e.okafor")
        monkeypatch.setenv("CUA_SECRET_OPERATOR_PASSWORD", "hunter2")

        _, evidence = self.replay(surface, lookup, live_server, tmp_path)
        written = (evidence.dir / "trace.jsonl").read_text(encoding="utf-8")
        written += (evidence.dir / "result.json").read_text(encoding="utf-8")
        assert "hunter2" not in written

    def test_the_configured_waiting_bounds_reach_the_surface(
            self, surface, lookup, live_server, tmp_path, monkeypatch):
        """Both bounds are engine config (§6), so both have to arrive. A knob that
        is loaded and never consulted is worse than no knob: changing it in
        runtime.yaml does nothing, and nothing says so."""
        monkeypatch.setenv("CUA_SECRET_OPERATOR_ID", "e.okafor")
        monkeypatch.setenv("CUA_SECRET_OPERATOR_PASSWORD", "irrelevant")

        runtime = RuntimeConfig.load("legacy-cu-portal")
        runtime.settle_timeout_ms = 2_500
        evidence = RunEvidence(run_id="run_20260814_000004", root=tmp_path)
        engine = ReplayEngine(surface, lookup, evidence=evidence, runtime=runtime)
        result = engine.run(self.params, live_server)

        assert result.status == "SUCCESS"
        assert surface.timeout_ms == runtime.step_timeout_ms
        assert surface.settle_timeout_ms == 2_500

    def test_drift_is_warned_about_not_failed_on(
            self, surface, lookup, live_server, tmp_path, monkeypatch):
        """Most app changes do not touch the recorded flow, so a mismatch is a
        note for the postmortem rather than a reason to refuse to run."""
        monkeypatch.setenv("CUA_SECRET_OPERATOR_ID", "e.okafor")
        monkeypatch.setenv("CUA_SECRET_OPERATOR_PASSWORD", "irrelevant")

        drifted = json.loads(json.dumps(lookup))
        drifted["capability"]["recorded_against"]["app_fingerprint"] = "legacy-cu-portal@1.0.0"

        result, _ = self.replay(surface, drifted, live_server, tmp_path)
        assert result.status == "SUCCESS"
        assert "drifted" in result.envelope["drift_warning"]

    def test_a_failed_checkpoint_produces_forensics(
            self, surface, lookup, live_server, tmp_path, monkeypatch):
        """§7: expected, observed, what was tried, a screenshot and a DOM snapshot —
        and no remediation advice, which the schema also refuses."""
        monkeypatch.setenv("CUA_SECRET_OPERATOR_ID", "e.okafor")
        monkeypatch.setenv("CUA_SECRET_OPERATOR_PASSWORD", "irrelevant")

        broken = json.loads(json.dumps(lookup))
        broken["steps"][0]["checkpoint"] = {"condition": "text_present",
                                            "value": "Wire Transfer Console"}

        result, evidence = self.replay(surface, broken, live_server, tmp_path)

        assert result.status == "HARD_FAILURE"
        payload = result.envelope["payload"]
        assert payload["failed_at_step"] == 1
        assert "Wire Transfer Console" in payload["expected"]
        assert Path(payload["screenshot"]).exists()
        assert Path(payload["dom_snapshot"]).exists()
        assert "suggested_fix" not in payload


@pytest.mark.browser
class TestBusinessOutcomes:
    """The three-class rule's spine: an expected business result is a terminal
    status the caller branches on, never a crash."""

    def replay_for(self, surface, lookup, live_server, tmp_path, member):
        evidence = RunEvidence(run_id=f"run_2026081400{member}", root=tmp_path)
        engine = ReplayEngine(surface, lookup, evidence=evidence,
                              runtime=RuntimeConfig.load("legacy-cu-portal"))
        return engine.run({"member_number": member}, live_server), evidence

    @pytest.fixture(autouse=True)
    def _credentials(self, monkeypatch):
        monkeypatch.setenv("CUA_SECRET_OPERATOR_ID", "e.okafor")
        monkeypatch.setenv("CUA_SECRET_OPERATOR_PASSWORD", "irrelevant")

    def test_an_absent_member_is_an_answer_not_a_crash(
            self, surface, lookup, live_server, tmp_path):
        result, _ = self.replay_for(surface, lookup, live_server, tmp_path, "99999")

        assert result.status == "BUSINESS_OUTCOME"
        payload = result.envelope["payload"]
        assert payload["outcome_code"] == "MEMBER_NOT_FOUND"
        assert payload["detected_at_step"] == 6
        assert validate_result(result.envelope)

    def test_a_denied_region_is_an_answer_not_a_crash(
            self, surface, lookup, live_server, tmp_path):
        result, _ = self.replay_for(surface, lookup, live_server, tmp_path, "67890")
        assert result.envelope["payload"]["outcome_code"] == "PERMISSION_DENIED"

    def test_the_outcome_preempts_the_failing_checkpoint(
            self, surface, lookup, live_server, tmp_path):
        """Step 6 expects 'Member Profile' and does not get it. A checkpoint-first
        engine calls that a hard failure; it is the answer. Nothing in the trace
        should record a failure at all."""
        _, evidence = self.replay_for(surface, lookup, live_server, tmp_path, "99999")
        events = [e["event"] for e in evidence.read_trace()]
        assert "business_outcome" in events
        assert "hard_failure" not in events

    def test_the_detail_says_what_matched_and_why_it_is_not_a_failure(
            self, surface, lookup, live_server, tmp_path):
        result, _ = self.replay_for(surface, lookup, live_server, tmp_path, "99999")
        detail = result.envelope["payload"]["detail"]
        assert "No member matches this number" in detail
        assert "Not a failure" in detail        # carried from outcomes.yaml `meaning`

    def test_detection_is_screenshotted(self, surface, lookup, live_server, tmp_path):
        """An answer someone may have to justify later is worth being able to
        show rather than assert."""
        result, _ = self.replay_for(surface, lookup, live_server, tmp_path, "67890")
        assert Path(result.envelope["payload"]["evidence"]).exists()

    def test_no_outputs_are_claimed(self, surface, lookup, live_server, tmp_path):
        """The flow ended before the read step, so the caller gets an outcome and
        no values — not zeros, not nulls dressed as data."""
        result, _ = self.replay_for(surface, lookup, live_server, tmp_path, "99999")
        assert "outputs" not in result.envelope["payload"]
        assert result.outputs == {}


@pytest.mark.browser
class TestRuntimeConditions:
    """The recoverable class (§6). A condition that the engine can handle is
    logged in the trace and never becomes a terminal status — the caller asked
    whether the capability succeeded, not whether the network had a bad afternoon."""

    @pytest.fixture(autouse=True)
    def _credentials(self, monkeypatch):
        monkeypatch.setenv("CUA_SECRET_OPERATOR_ID", "e.okafor")
        monkeypatch.setenv("CUA_SECRET_OPERATOR_PASSWORD", "irrelevant")

    def replay(self, surface, lookup, live_server, tmp_path, chaos):
        evidence = RunEvidence(run_id=f"run_2026081401_{chaos}", root=tmp_path)
        engine = ReplayEngine(surface, lookup, evidence=evidence, chaos=chaos,
                              runtime=RuntimeConfig.load("legacy-cu-portal"))
        return engine.run({"member_number": "23456"}, live_server), evidence

    @pytest.mark.slow
    def test_a_slow_page_is_retried_not_failed(
            self, surface, lookup, live_server, tmp_path):
        """An 8s stall is weather, not an outcome. The run still succeeds and the
        recovery is visible to whoever is debugging."""
        result, evidence = self.replay(surface, lookup, live_server, tmp_path, "slow")

        assert result.status == "SUCCESS"
        assert result.outputs == {"savings_balance": 18240.55}

        events = [e["event"] for e in evidence.read_trace()]
        assert "action_timeout" in events, "the per-attempt bound should have been hit"

        recovered = [e for e in evidence.read_trace() if e["event"] == "recovered"]
        assert [e["condition"] for e in recovered] == ["SLOW_LOAD"]
        assert recovered[0]["attempts"] >= 2

    def test_a_timed_out_action_is_re_checked_never_re_sent(
            self, surface, lookup, live_server, tmp_path):
        """A submitted form can be processing while the browser gives up waiting.
        Re-sending is how automation double-posts, so the recovery is a re-check."""
        _, evidence = self.replay(surface, lookup, live_server, tmp_path, "slow")
        steps = [e for e in evidence.read_trace() if e["event"] == "step"]
        executed = [e["step"] for e in steps]

        assert executed == sorted(set(executed)), "a step was executed twice"
        assert len(executed) == len(lookup["steps"]), "every step ran, exactly once"

    def test_an_error_page_is_a_hard_failure_and_is_not_retried(
            self, surface, lookup, live_server, tmp_path):
        """A 500 is the app saying it is broken. Asking again is not a plan."""
        result, evidence = self.replay(surface, lookup, live_server, tmp_path, "error")

        assert result.status == "HARD_FAILURE"
        assert "error page" in result.envelope["payload"]["observed"]
        assert Path(result.envelope["payload"]["dom_snapshot"]).exists()
        assert not [e for e in evidence.read_trace() if e["event"] == "recovered"]

    def test_an_unknown_dialog_stops_and_hands_over(
            self, surface, lookup, live_server, tmp_path):
        """Never guess at a modal we did not expect: an unrecognised dialog on a
        banking screen is exactly where clicking the plausible button does damage."""
        result, _ = self.replay(surface, lookup, live_server, tmp_path, "dialog")

        assert result.status == "NEEDS_INTERVENTION"
        payload = result.envelope["payload"]
        assert payload["reason"] == "UNEXPECTED_DIALOG"
        assert "Scheduled maintenance" in payload["detail"]
        assert payload["session_id"], "the live session must be handed over, not a fresh one"
        assert Path(payload["screenshot"]).exists()

    def test_a_declared_interstitial_is_dismissed_and_the_run_continues(
            self, surface, lookup, live_server, tmp_path):
        """The same modal on the same page, and the only difference is whether it
        was declared: undeclared it stops the run and hands over, declared it is
        dismissed, logged and stepped past. That is the whole of KNOWN_INTERSTITIAL
        — the engine's rule is structural, and the *judgement* lives in config.

        runtime.yaml leaves `known` empty on purpose, because the maintenance
        notice is the trigger for the §9 handoff demo and must stay unknown there.
        So the declaration is supplied here instead, which is also the honest test:
        it proves an operator can turn a stop into a recovery without touching
        engine code.
        """
        from cua.replay.runtime import DialogPolicy, KnownInterstitial

        runtime = RuntimeConfig.load("legacy-cu-portal")
        runtime.dialogs = DialogPolicy(
            detect_selector=runtime.dialogs.detect_selector,
            known=(KnownInterstitial(
                name="MAINTENANCE_NOTICE",
                detect={"condition": "text_present", "value": "Scheduled maintenance"},
                dismiss={"strategies": [
                    {"kind": "label", "value": "Acknowledge", "role": "button"}]}),))

        evidence = RunEvidence(run_id="run_2026081401_known", root=tmp_path)
        engine = ReplayEngine(surface, lookup, evidence=evidence, chaos="dialog",
                              runtime=runtime)
        result = engine.run({"member_number": "23456"}, live_server)

        assert result.status == "SUCCESS", "a declared interstitial is not a stop"
        assert result.outputs == {"savings_balance": 18240.55}

        recovered = [e for e in evidence.read_trace() if e["event"] == "recovered"]
        assert [e["condition"] for e in recovered] == ["KNOWN_INTERSTITIAL"]
        assert recovered[0]["name"] == "MAINTENANCE_NOTICE"
        assert "unknown_dialog" not in [e["event"] for e in evidence.read_trace()]

    def test_an_expired_session_is_recovered_by_the_declared_routine(
            self, surface, lookup, live_server, tmp_path):
        """Recoverable only because a re-login routine exists for this app (§6),
        and that routine is executed by this same interpreter."""
        result, evidence = self.replay(surface, lookup, live_server, tmp_path, "session")

        assert result.status == "SUCCESS"
        events = [e["event"] for e in evidence.read_trace()]
        assert "session_recovery_started" in events
        assert "recovery_step" in events
        assert [e["condition"] for e in evidence.read_trace()
                if e["event"] == "recovered"] == ["SESSION_EXPIRED"]

    def test_recovery_never_becomes_a_terminal_status(
            self, surface, lookup, live_server, tmp_path):
        """The spine of the three-class rule: recovered conditions are logged, and
        the caller still just sees SUCCESS."""
        result, _ = self.replay(surface, lookup, live_server, tmp_path, "session")
        assert result.status == "SUCCESS"
        assert result.recoveries
        assert "recover" not in json.dumps(result.envelope["payload"]).lower()

    def test_chaos_is_armed_mid_flow_not_at_the_start(
            self, surface, lookup, live_server, tmp_path):
        """Armed on the opening navigation, every condition lands on the login page
        where a stall is absorbed by the navigation timeout and a cleared session
        changes nothing — the run goes green and proves the opposite of its claim."""
        _, evidence = self.replay(surface, lookup, live_server, tmp_path, "dialog")
        armed = next(e for e in evidence.read_trace() if e["event"] == "chaos_armed")
        assert armed["before_step"] > 1

    def test_a_checkpoint_failure_before_sign_on_is_not_an_expired_session(
            self, surface, lookup, live_server, tmp_path):
        """Being on the login page is not evidence of anything: it is where every
        capability starts. A session can only be lost if one was established."""
        broken = json.loads(json.dumps(lookup))
        broken["steps"][0]["checkpoint"] = {"condition": "text_present",
                                            "value": "Wire Transfer Console"}
        evidence = RunEvidence(run_id="run_2026081401_early", root=tmp_path)
        engine = ReplayEngine(surface, broken, evidence=evidence,
                              runtime=RuntimeConfig.load("legacy-cu-portal"))
        result = engine.run({"member_number": "23456"}, live_server)

        assert result.status == "HARD_FAILURE"
        assert "session_recovery_started" not in [
            e["event"] for e in evidence.read_trace()]

    def test_an_unrecoverable_session_escalates(
            self, surface, lookup, live_server, tmp_path):
        """Strip the routine and the same condition becomes an intervention — the
        difference is entirely whether a recovery was declared."""
        from cua.replay.runtime import SessionPolicy

        evidence = RunEvidence(run_id="run_2026081401_nosession", root=tmp_path)
        runtime = RuntimeConfig.load("legacy-cu-portal")
        runtime.session = SessionPolicy(detect=runtime.session.detect, relogin=())
        engine = ReplayEngine(surface, lookup, evidence=evidence, chaos="session",
                              runtime=runtime)
        result = engine.run({"member_number": "23456"}, live_server)

        assert result.status == "NEEDS_INTERVENTION"
        assert result.envelope["payload"]["reason"] == "SESSION_EXPIRED"
        assert "no re-login routine" in result.envelope["payload"]["detail"]


@pytest.mark.browser
class TestRiskGates:

    def test_a_mutating_step_stops_without_standing_approval(
            self, surface, sub_account, live_server, tmp_path, monkeypatch):
        monkeypatch.setenv("CUA_SECRET_OPERATOR_ID", "e.okafor")
        monkeypatch.setenv("CUA_SECRET_OPERATOR_PASSWORD", "irrelevant")

        evidence = RunEvidence(run_id="run_20260814_000002", root=tmp_path)
        engine = ReplayEngine(surface, sub_account, evidence=evidence,
                              runtime=RuntimeConfig.load("legacy-cu-portal"))
        result = engine.run({
            "member_number": "23456", "account_type": "Holiday Club",
            "account_nickname": "Vacation fund", "initial_deposit": "150.00",
        }, live_server)

        assert result.status == "NEEDS_INTERVENTION"
        payload = result.envelope["payload"]
        assert payload["reason"] == "MUTATION_NOT_APPROVED"
        assert payload["control"] == "HUMAN"
        assert payload["session_id"]

        from target_app import data
        assert data.sub_accounts_for("23456") == [], "the gate reported but did not stop"

    def test_an_irreversible_step_stops_even_with_standing_approval(
            self, surface, sub_account, live_server, tmp_path, monkeypatch):
        """A blanket --approve-mutations is a statement about a class of actions,
        not consent to a particular unrepeatable one."""
        monkeypatch.setenv("CUA_SECRET_OPERATOR_ID", "e.okafor")
        monkeypatch.setenv("CUA_SECRET_OPERATOR_PASSWORD", "irrelevant")

        evidence = RunEvidence(run_id="run_20260814_000003", root=tmp_path)
        engine = ReplayEngine(surface, sub_account, evidence=evidence,
                              runtime=RuntimeConfig.load("legacy-cu-portal"),
                              approve_mutations=True)
        result = engine.run({
            "member_number": "23456", "account_type": "Holiday Club",
            "account_nickname": "Vacation fund", "initial_deposit": "150.00",
        }, live_server)

        assert result.status == "NEEDS_INTERVENTION"
        assert result.envelope["payload"]["reason"] == "IRREVERSIBLE_STEP"
        assert result.envelope["steps_completed"] == 11, "should have run up to the gate"

        from target_app import data
        assert data.sub_accounts_for("23456") == [], "the gate reported but did not stop"
