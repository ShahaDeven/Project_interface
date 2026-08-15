"""Tests for the artifact and result contracts (DESIGN §5, §7).

A schema without tests is a document, not a guarantee — the failure mode is a
schema that accepts everything and nobody notices. So each test here names a
specific malformed document that **must** be rejected, and says why that rejection
matters at run time.

The valid fixture doubles as a worked example of the schema: `lookup_member_balance`
recorded against the target app in this repo.
"""

import copy
import json
from pathlib import Path

import pytest

from cua.contracts import (
    ContractError, load_artifact, save_artifact, validate_artifact, validate_result,
)

FIXTURE = Path(__file__).parent / "fixtures" / "lookup_member_balance.json"


@pytest.fixture
def artifact():
    with open(FIXTURE, encoding="utf-8") as handle:
        return json.load(handle)


def rejects(document, *expected_fragments):
    """Assert the document is refused, and that the message points at the cause."""
    with pytest.raises(ContractError) as raised:
        validate_artifact(document)
    message = str(raised.value)
    for fragment in expected_fragments:
        assert fragment in message, f"expected {fragment!r} in:\n{message}"


# =============================================================================
# The fixture itself
# =============================================================================

class TestValidArtifact:

    def test_the_worked_example_validates(self, artifact):
        assert validate_artifact(artifact) is artifact

    def test_it_round_trips_through_disk(self, artifact, tmp_path):
        path = save_artifact(artifact, tmp_path / "out" / "cap.json")
        assert load_artifact(path) == artifact

    def test_saving_an_invalid_artifact_writes_nothing(self, artifact, tmp_path):
        """Validate before write, so a bad artifact never reaches /capabilities."""
        artifact["capability"]["version"] = "one point oh"
        path = tmp_path / "cap.json"
        with pytest.raises(ContractError):
            save_artifact(artifact, path)
        assert not path.exists()

    def test_every_step_declares_a_risk(self, artifact):
        """The policy gate hangs off this field, so it can never be absent."""
        assert all("risk" in step for step in artifact["steps"])

    def test_no_credential_literal_is_stored(self, artifact):
        """Credentials are referenced, never recorded (DESIGN §8)."""
        blob = json.dumps(artifact)
        assert "{secrets.operator_id}" in blob
        assert "{secrets.operator_password}" in blob
        for step in artifact["steps"]:
            value = step.get("value", "")
            assert not value or value.startswith("{"), f"literal value {value!r} in step {step['id']}"


# =============================================================================
# Shape — what JSON Schema owns
# =============================================================================

class TestStepShape:

    def test_coordinates_without_a_nearby_text_check_are_rejected(self, artifact):
        """The never-click-blind rule is enforced by the contract, not by
        convention. A stale coordinate must fail, not click whatever moved there."""
        strategies = artifact["steps"][3]["target"]["strategies"]
        del strategies[2]["verify_text_nearby"]
        rejects(artifact, "steps/3")

    def test_navigate_without_a_checkpoint_is_rejected(self, artifact):
        """'The navigation worked' is verified, never assumed."""
        del artifact["steps"][0]["checkpoint"]
        rejects(artifact, "steps/0", "checkpoint")

    def test_click_without_a_checkpoint_is_rejected(self, artifact):
        del artifact["steps"][3]["checkpoint"]
        rejects(artifact, "steps/3", "checkpoint")

    def test_type_without_a_value_is_rejected(self, artifact):
        del artifact["steps"][1]["value"]
        rejects(artifact, "steps/1", "value")

    def test_read_without_an_output_is_rejected(self, artifact):
        """A read that does not say what it fills is unexecutable."""
        del artifact["steps"][6]["output"]
        rejects(artifact, "steps/6", "output")

    def test_navigate_carrying_a_target_is_rejected(self, artifact):
        """Fields are action-specific: a step cannot be half one thing, half another."""
        artifact["steps"][0]["target"] = {"strategies": [{"kind": "label", "value": "x"}]}
        rejects(artifact, "steps/0")

    def test_empty_strategy_chain_is_rejected(self, artifact):
        artifact["steps"][3]["target"]["strategies"] = []
        rejects(artifact, "steps/3")

    def test_unknown_action_is_rejected(self, artifact):
        """`done` and `stuck` are loop control, not replayable steps."""
        artifact["steps"][3]["action"] = "done"
        rejects(artifact, "steps/3")

    def test_unknown_risk_class_is_rejected(self, artifact):
        artifact["steps"][3]["risk"] = "probably_fine"
        rejects(artifact, "steps/3", "risk")

    def test_unknown_field_is_rejected(self, artifact):
        """Strict by design: a typo'd key is a silently ignored instruction."""
        artifact["steps"][3]["retry_forever"] = True
        rejects(artifact, "steps/3")


class TestDocumentShape:

    def test_non_semver_capability_version_is_rejected(self, artifact):
        artifact["capability"]["version"] = "1.0"
        rejects(artifact, "capability/version")

    def test_fingerprint_without_a_build_is_rejected(self, artifact):
        """app@build, so a mismatch is detectable. A bare name never changes."""
        artifact["capability"]["recorded_against"]["app_fingerprint"] = "legacy-cu-portal"
        rejects(artifact, "app_fingerprint")

    def test_bad_recorded_at_is_rejected(self, artifact):
        artifact["capability"]["recorded_against"]["recorded_at"] = "last Tuesday"
        rejects(artifact, "recorded_at")

    def test_outcome_without_a_meaning_is_rejected(self, artifact):
        """If you cannot say why it is not a failure, it is a failure."""
        del artifact["expected_outcomes"][0]["meaning"]
        rejects(artifact, "expected_outcomes/0", "meaning")

    def test_lowercase_outcome_code_is_rejected(self, artifact):
        artifact["expected_outcomes"][0]["code"] = "member_not_found"
        rejects(artifact, "expected_outcomes/0")

    def test_missing_success_checkpoint_is_rejected(self, artifact):
        del artifact["success"]
        rejects(artifact, "success")

    def test_no_steps_is_rejected(self, artifact):
        artifact["steps"] = []
        rejects(artifact, "steps")

    def test_wrong_schema_version_is_rejected(self, artifact):
        artifact["schema_version"] = "2.0"
        rejects(artifact, "schema_version")


# =============================================================================
# Referential integrity — what JSON Schema cannot express
# =============================================================================

class TestIntegrity:

    def test_output_with_no_producing_read_step_is_rejected(self, artifact):
        artifact["outputs"]["credit_score"] = {
            "type": "integer", "description": "Credit score.", "source_step": 9,
        }
        rejects(artifact, "outputs/credit_score", "no read step fills it")

    def test_source_step_pointing_at_the_wrong_step_is_rejected(self, artifact):
        """Silently wrong provenance is worse than a missing field: the artifact
        stays reviewable-looking while documenting a lie."""
        artifact["outputs"]["member_name"]["source_step"] = 3
        rejects(artifact, "outputs/member_name", "source_step")

    def test_read_into_an_undeclared_output_is_rejected(self, artifact):
        artifact["steps"][6]["output"] = "not_declared"
        rejects(artifact, "not a declared output")

    def test_duplicate_reads_into_one_output_are_rejected(self, artifact):
        artifact["steps"][7]["output"] = "member_name"
        rejects(artifact, "more than one read step")

    def test_non_contiguous_step_ids_are_rejected(self, artifact):
        """Step ids address failures and interventions; gaps make
        'failed_at_step: 4' ambiguous."""
        artifact["steps"][4]["id"] = 99
        rejects(artifact, "contiguous")

    def test_reference_to_an_undeclared_input_is_rejected(self, artifact):
        artifact["steps"][4]["value"] = "{inputs.branch_code}"
        rejects(artifact, "steps/5", "not declared")

    def test_undeclared_secret_is_rejected(self, artifact):
        """A capability must declare what credentials it needs — names, never values."""
        artifact["capability"]["requires_secrets"] = ["operator_id"]
        rejects(artifact, "requires_secrets")

    def test_unknown_template_is_rejected(self, artifact):
        artifact["steps"][0]["url"] = "{host}/login"
        rejects(artifact, "unknown template")

    def test_duplicate_outcome_codes_are_rejected(self, artifact):
        artifact["expected_outcomes"][1]["code"] = "MEMBER_NOT_FOUND"
        rejects(artifact, "duplicate codes")

    def test_all_problems_are_reported_at_once(self, artifact):
        """Fixing an artifact one error per run is miserable."""
        artifact["outputs"]["member_name"]["source_step"] = 2
        artifact["expected_outcomes"][1]["code"] = "MEMBER_NOT_FOUND"
        with pytest.raises(ContractError) as raised:
            validate_artifact(artifact)
        assert len(raised.value.problems) == 2


# =============================================================================
# Result envelope
# =============================================================================

def envelope(**overrides):
    base = {
        "run_id": "run_20260814_093012",
        "capability": "lookup_member_balance",
        "capability_version": "1.0.0",
        "mode": "replay",
        "status": "SUCCESS",
        "inputs": {"member_id": "23456"},
        "started_at": "2026-08-14T09:30:12Z",
        "ended_at": "2026-08-14T09:30:19Z",
        "steps_completed": 8,
        "steps_total": 8,
        "llm_call_count": 0,
        "evidence_path": "evidence/run_20260814_093012/",
        "payload": {
            "outputs": {"member_name": "Marcus Bell", "savings_balance": 18240.55},
            "checkpoint_verified": True,
        },
    }
    base.update(overrides)
    return base


class TestResultEnvelope:

    def test_success_envelope_validates(self):
        assert validate_result(envelope())

    def test_replay_may_not_report_model_calls(self):
        """The system's central claim, enforced by the contract rather than
        asserted in prose: a replay result with a non-zero count is invalid."""
        with pytest.raises(ContractError) as raised:
            validate_result(envelope(llm_call_count=3))
        assert "llm_call_count" in str(raised.value)

    def test_discovery_may_report_model_calls(self):
        assert validate_result(envelope(mode="discovery", llm_call_count=11))

    def test_business_outcome_envelope_validates(self):
        assert validate_result(envelope(
            status="BUSINESS_OUTCOME",
            steps_completed=6,
            payload={
                "outcome_code": "MEMBER_NOT_FOUND",
                "detected_at_step": 6,
                "detail": "Search for member 99999 returned 'No member matches this number'.",
                "evidence": "evidence/run_20260814_093012/step6_outcome.png",
            }))

    def test_needs_intervention_envelope_validates(self):
        assert validate_result(envelope(
            status="NEEDS_INTERVENTION",
            steps_completed=4,
            payload={
                "reason": "UNEXPECTED_DIALOG",
                "detail": "Modal after step 4: 'Scheduled maintenance at 6 PM'.",
                "paused_at_step": 4,
                "screenshot": "evidence/run_20260814_093012/intervention_step4.png",
                "session_id": "browser_sess_7f2a",
                "requested_action": "Dismiss or defer; resume when Member Profile is visible.",
                "control": "HUMAN",
            }))

    def test_hard_failure_envelope_validates(self):
        assert validate_result(envelope(
            status="HARD_FAILURE",
            steps_completed=5,
            payload={
                "failed_at_step": 6,
                "action_attempted": "click",
                "expected": "checkpoint 'Member Profile' visible within 10s",
                "observed": "page title 'HTTP 500 — Internal Server Error'",
                "strategies_tried": ["label: Look Up", "structural: form input[type=submit]",
                                     "coordinates+verify: Member number"],
                "screenshot": "evidence/run_20260814_093012/failure_step6.png",
                "dom_snapshot": "evidence/run_20260814_093012/failure_step6.html",
            }))

    def test_payload_must_match_the_status(self):
        """The envelope is common; the payload is not. A caller branching on
        status must be able to trust the shape it gets."""
        with pytest.raises(ContractError):
            validate_result(envelope(status="HARD_FAILURE"))

    def test_hard_failure_may_not_carry_a_remediation_suggestion(self):
        """If the engine knew the fix it would be a recoverable condition. A
        suggestion here is a guess wearing the costume of an instruction."""
        payload = {
            "failed_at_step": 6,
            "action_attempted": "click",
            "expected": "checkpoint 'Member Profile'",
            "observed": "HTTP 500",
            "suggested_fix": "try again later",
        }
        with pytest.raises(ContractError):
            validate_result(envelope(status="HARD_FAILURE", payload=payload))

    def test_intervention_record_validates(self):
        """A list since 4b: a capability with a mutating step and an irreversible
        one pauses twice, so a single-entry record would drop the handoff that
        approved a write. `operator` is per run — the same person answers the
        prompt they are sitting at."""
        assert validate_result(envelope(intervention_record={
            "operator": "E. Okafor",
            "interventions": [{
                "paused_at_step": 4,
                "reason": "UNEXPECTED_DIALOG",
                "when": "after_step",
                "requested_action": "Dismiss or defer it, then resume.",
                "paused_at": "2026-08-14T09:31:12Z",
                "control_returned_at": "2026-08-14T09:31:40Z",
                "decision": "resume",
                "resolution": "verified",
                "human_actions": [{
                    "recorded_at": "2026-08-14T09:31:22Z",
                    "url": "http://127.0.0.1:5000/member/23456",
                    "summary": "Dismissed the maintenance notice.",
                }],
                "post_resume_checkpoint": {
                    "condition": "text_present", "value": "Member Profile",
                    "passed": True,
                },
            }],
        }))

    def test_unknown_status_is_rejected(self):
        with pytest.raises(ContractError):
            validate_result(envelope(status="PROBABLY_FINE"))

    def test_missing_evidence_path_is_rejected(self):
        broken = envelope()
        del broken["evidence_path"]
        with pytest.raises(ContractError):
            validate_result(broken)


# =============================================================================
# CLI
# =============================================================================

class TestValidateCommand:

    def test_valid_artifact_exits_zero(self, capsys):
        from cua.__main__ import main
        assert main(["validate", str(FIXTURE)]) == 0
        assert "OK" in capsys.readouterr().out

    def test_invalid_artifact_exits_nonzero(self, artifact, tmp_path, capsys):
        from cua.__main__ import main
        artifact["steps"][0]["action"] = "teleport"
        path = tmp_path / "broken.json"
        path.write_text(json.dumps(artifact), encoding="utf-8")
        assert main(["validate", str(path)]) == 1
        assert "FAIL" in capsys.readouterr().err

    def test_replay_refuses_bad_parameters_without_launching_anything(self, capsys):
        """Every subcommand is built now, so this no longer guards a stub — it
        guards the property that made the stub check worth having: a CLI mistake
        must cost nothing. Parameters are validated against the capability's
        contract before a browser opens, and this test would hang or spend money
        if that order ever inverted."""
        from cua.__main__ import main
        code = main(["replay", "lookup_member_balance", "--param", "member_id=23456"])
        assert code == 1
        error = capsys.readouterr().err
        assert "unknown parameter(s) ['member_id']" in error
        assert "member_number" in error, "should name what the capability does declare"

    def test_discover_refuses_an_off_allowlist_target_without_spending_anything(self, capsys):
        """Fail in cost order: no browser, no client, no tokens."""
        from cua.__main__ import main
        code = main(["discover", "--goal", "x", "--target", "https://example.com",
                     "--save-as", "thing"])
        assert code == 1
        assert "not in the allowlist" in capsys.readouterr().err

    def test_discover_refuses_a_bad_capability_name(self, capsys):
        from cua.__main__ import main
        code = main(["discover", "--goal", "x", "--target", "http://127.0.0.1:5000",
                     "--save-as", "Not Snake Case"])
        assert code == 1
        assert "snake_case" in capsys.readouterr().err
