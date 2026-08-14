"""Tests for the distiller and risk policy (DESIGN §4, §5, §8).

The trace fixture below mirrors `evidence/run_20260813_233258` — the first real
discovery run — event for event, so these tests exercise the shapes the executor
actually produces rather than shapes that would be convenient.

Everything the distiller emits is validated against the artifact schema, so a
regression shows up as a contract violation rather than as a plausible-looking
file that fails months later at replay.
"""

import pytest

from cua.contracts import ContractError, validate_artifact
from cua.distill import DistillationError, distil, outcomes_for, slugify
from cua.policy import RiskPolicy, mask, shape_of

BASE = "http://127.0.0.1:5000"
GOAL = "look up member 12345 and read their savings balance"


def strategies(label, role="textbox"):
    return [
        {"kind": "label", "value": label, "role": role},
        {"kind": "structural", "value": f"body > table > tr > td > input[{label}]"},
        {"kind": "coordinates", "value": [412, 288], "verify_text_nearby": label},
    ]


def target(label, role="textbox", form=None, frame=None):
    out = {"role": role, "label": label, "kind": "interactive",
           "structural": f"body > table > tr > td > input[{label}]",
           "center": [412, 288], "box": [0, 0, 80, 20], "frame": frame,
           "form": form, "strategies": strategies(label, role)}
    return out


LOGIN_FORM = {"method": "post", "action": "/login", "submits": True}
SEARCH_FORM = {"method": "get", "action": "/search", "submits": True}


def real_shaped_trace():
    return [
        {"event": "run_started", "mode": "discovery", "goal": GOAL, "base_url": BASE,
         "model": "claude-sonnet-5"},
        {"event": "step", "step": 1, "action": "navigate", "reason": "Start at the root",
         "url": BASE, "after": {"url": f"{BASE}/login", "marker": "Operator Login"}},
        {"event": "step", "step": 2, "action": "type", "reason": "Enter operator ID",
         "value": "{secrets.operator_id}", "target": target("Operator ID")},
        {"event": "step", "step": 3, "action": "type", "reason": "Enter password",
         "value": "{secrets.operator_password}", "target": target("Password", "password")},
        {"event": "step", "step": 4, "action": "click", "reason": "Sign on",
         "target": target("Sign On", "button", form=LOGIN_FORM),
         "after": {"url": f"{BASE}/search", "marker": "Member Lookup"}},
        {"event": "step", "step": 5, "action": "type", "reason": "Enter member number",
         "value": "12345", "target": target("Member number")},
        {"event": "step", "step": 6, "action": "click", "reason": "Submit lookup",
         "target": target("Look Up", "button", form=SEARCH_FORM),
         "after": {"url": f"{BASE}/member/12345", "marker": "Member Profile"}},
        {"event": "step", "step": 7, "action": "read", "reason": "Capture balance",
         "output": "savings_balance", "value_shape": "currency", "value_masked": "$•••••",
         "target": target("Savings balance", "cell")},
        {"event": "success_verified", "step": 8, "success_evidence": "Member Profile"},
        {"event": "run_finished", "status": "SUCCESS", "llm_call_count": 8},
    ]


def build(trace=None, **overrides):
    settings = dict(
        capability_name="lookup_member_balance",
        app="legacy-cu-portal",
        app_fingerprint="legacy-cu-portal@4.2.1",
        recorded_at="2026-08-14T03:32:58Z",
        run_id="run_20260813_233258",
        outcomes=outcomes_for("legacy-cu-portal"),
        risk_policy=RiskPolicy(
            read_only_routes=[r"^/login$", r"^/search$"],
            mutating_routes=[r"^/member/[0-9]{5}/sub-account/new$"],
            irreversible_routes=[r"^/member/[0-9]{5}/sub-account/confirm$"]),
    )
    settings.update(overrides)
    return distil(trace if trace is not None else real_shaped_trace(), **settings)


@pytest.fixture
def artifact():
    return build()


# =============================================================================
# The whole pipeline
# =============================================================================

class TestDistillation:

    def test_the_result_is_a_valid_capability(self, artifact):
        assert validate_artifact(artifact)
        assert artifact["capability"]["name"] == "lookup_member_balance"
        assert len(artifact["steps"]) == 7

    def test_terminal_tools_do_not_become_steps(self, artifact):
        """`done` is loop control, not a replayable action."""
        assert {s["action"] for s in artifact["steps"]} == {
            "navigate", "click", "type", "read"}

    def test_step_ids_are_renumbered_contiguously(self, artifact):
        assert [s["id"] for s in artifact["steps"]] == [1, 2, 3, 4, 5, 6, 7]


class TestParameterisation:

    def test_a_goal_derived_literal_becomes_an_input(self, artifact):
        """12345 was given in the goal, so it is a parameter."""
        assert "member_number" in artifact["inputs"]
        typed = next(s for s in artifact["steps"] if s["id"] == 5)
        assert typed["value"] == "{inputs.member_number}"

    def test_the_pattern_is_derived_from_the_observed_value(self, artifact):
        """A heuristic from one sample, and one that fails closed: a wrong pattern
        rejects the input at the CLI rather than half-completing a flow."""
        assert artifact["inputs"]["member_number"]["pattern"] == "^[0-9]{5}$"

    @pytest.mark.parametrize("goal_fragment,typed,expected_input", [
        ("with an opening deposit of 150.00", "150.00", True),
        ("account type 'Holiday Club'", "Holiday Club", True),
        ("open a sub-account", "Vacation fund", False),
    ])
    def test_which_typed_values_become_parameters(self, goal_fragment, typed,
                                                  expected_input):
        """Decimals must survive as one token: '150.00' split into '150' and '00'
        matches nothing the agent typed, and the amount stays hardcoded."""
        trace = real_shaped_trace()
        trace[0]["goal"] = f"{GOAL} {goal_fragment}"
        trace[5]["value"] = typed
        step = next(s for s in build(trace)["steps"] if s["id"] == 5)
        assert step["value"].startswith("{inputs.") is expected_input

    def test_a_value_not_from_the_goal_stays_literal(self):
        """Only what the caller supplied is parameterisable; a value the agent
        chose is part of the flow."""
        trace = real_shaped_trace()
        trace[5]["value"] = "Vacation fund"
        artifact = build(trace)
        assert next(s for s in artifact["steps"] if s["id"] == 5)["value"] == "Vacation fund"
        assert artifact["inputs"] == {}

    def test_credentials_stay_tokens_and_are_declared(self, artifact):
        assert artifact["capability"]["requires_secrets"] == [
            "operator_id", "operator_password"]
        values = [s.get("value") for s in artifact["steps"]]
        assert "{secrets.operator_id}" in values
        assert "{secrets.operator_password}" in values

    def test_the_base_url_is_templated(self, artifact):
        assert next(s for s in artifact["steps"] if s["id"] == 1)["url"] == "{base_url}"

    def test_the_recorded_literal_disappears_from_the_description(self, artifact):
        """A description reading 'look up member 12345' tells a calling agent this
        capability looks up member 12345 — which is what it no longer does."""
        description = artifact["capability"]["description"]
        assert "12345" not in description
        assert "{inputs.member_number}" in description

    def test_the_recorded_literal_disappears_from_step_reasons(self):
        trace = real_shaped_trace()
        trace[7]["reason"] = "Capture the savings balance for member 12345"
        artifact = build(trace)
        reason = next(s for s in artifact["steps"] if s["id"] == 7)["reason"]
        assert "12345" not in reason

    def test_nothing_in_the_artifact_names_the_recorded_member(self, artifact):
        """The whole-file version of the rule, since a literal can hide in a field
        nobody thought to check."""
        import json
        assert "12345" not in json.dumps(artifact)


class TestOutputs:

    def test_a_read_becomes_a_typed_output(self, artifact):
        balance = artifact["outputs"]["savings_balance"]
        assert balance["type"] == "number"
        assert balance["parse"] == "currency"
        assert balance["source_step"] == 7

    def test_typing_comes_from_shape_never_from_the_figure(self):
        """The trace holds no balance (§8), so the contract must be buildable from
        the shape alone."""
        trace = real_shaped_trace()
        assert all("value" not in e for e in trace if e.get("action") == "read")
        assert build(trace)["outputs"]["savings_balance"]["parse"] == "currency"

    def test_an_integer_read_types_as_integer(self):
        trace = real_shaped_trace()
        trace[7]["value_shape"] = "integer"
        trace[7]["output"] = "credit_score"
        assert build(trace)["outputs"]["credit_score"]["type"] == "integer"


class TestCheckpoints:

    def test_page_text_is_preferred(self, artifact):
        """A URL can be right while the page behind it is an error."""
        assert next(s for s in artifact["steps"] if s["id"] == 1)["checkpoint"] == {
            "condition": "text_present", "value": "Operator Login"}
        assert next(s for s in artifact["steps"] if s["id"] == 6)["checkpoint"] == {
            "condition": "text_present", "value": "Member Profile"}

    def test_url_is_the_fallback_when_no_text_qualifies(self):
        """Never invent a checkpoint: assert something weaker rather than
        something unverifiable."""
        trace = real_shaped_trace()
        trace[6]["after"]["marker"] = None
        step = next(s for s in build(trace)["steps"] if s["id"] == 6)
        assert step["checkpoint"] == {"condition": "url_matches",
                                      "value": "/member/{inputs.member_number}"}

    def test_success_checkpoint_is_the_verified_evidence(self, artifact):
        assert artifact["success"]["checkpoint"]["value"] == "Member Profile"

    def test_typing_steps_carry_no_checkpoint(self, artifact):
        """Filling a field does not transition the page; asserting one would be
        theatre."""
        assert "checkpoint" not in next(s for s in artifact["steps"] if s["id"] == 2)


class TestRisk:

    def test_a_read_only_flow_is_classified_read_only(self, artifact):
        assert {s["risk"] for s in artifact["steps"]} == {"read_only"}

    def test_login_post_is_not_a_mutation(self, artifact):
        """Signing in POSTs and creates nothing. Classifying it as mutating would
        make every capability require --approve-mutations."""
        assert next(s for s in artifact["steps"] if s["id"] == 4)["risk"] == "read_only"

    def test_a_post_to_a_mutating_route_is_mutating(self):
        trace = real_shaped_trace()
        trace[6]["target"]["form"] = {"method": "post",
                                      "action": "/member/12345/sub-account/new"}
        assert next(s for s in build(trace)["steps"] if s["id"] == 6)["risk"] == "mutating"

    def test_a_post_to_an_irreversible_route_is_irreversible(self):
        trace = real_shaped_trace()
        trace[6]["target"]["form"] = {"method": "post",
                                      "action": "/member/12345/sub-account/confirm"}
        assert next(s for s in build(trace)["steps"] if s["id"] == 6)["risk"] == "irreversible"

    def test_an_unrecognised_post_defaults_to_mutating(self):
        """Over-classifying costs an approval flag; under-classifying costs an
        unreviewed write to a member's account."""
        policy = RiskPolicy(read_only_routes=[r"^/login$"])
        assert policy.classify("click", {"method": "post", "action": "/who/knows"}) == "mutating"

    def test_clicking_a_control_that_does_not_submit_is_read_only(self):
        """Every control on a POST form belongs to that form, but only a submit
        control submits it. Without this, clicking a dropdown reads as a mutation
        and a capability reports five mutating steps when it has one."""
        policy = RiskPolicy(mutating_routes=[r"^/member/[0-9]{5}/sub-account/new$"])
        dropdown = {"method": "post", "action": "/member/12345/sub-account/new",
                    "submits": False}
        assert policy.classify("click", dropdown) == "read_only"

    def test_an_unflagged_form_is_assumed_to_submit(self):
        """Conservative default for a trace recorded before the flag existed."""
        policy = RiskPolicy(mutating_routes=[r"^/anything$"])
        assert policy.classify("click", {"method": "post", "action": "/anything"}) == "mutating"

    def test_a_get_can_never_be_a_mutation(self):
        policy = RiskPolicy(mutating_routes=[r"^/anything$"])
        assert policy.classify("click", {"method": "get", "action": "/anything"}) == "read_only"


class TestDeclaredOutcomes:

    def test_app_outcomes_are_attached(self, artifact):
        codes = {o["code"] for o in artifact["expected_outcomes"]}
        assert {"MEMBER_NOT_FOUND", "PERMISSION_DENIED"} <= codes

    def test_outcomes_the_flow_cannot_reach_are_not_attached(self, artifact):
        """A read-only lookup never touches the sub-account routes, so declaring a
        deposit rule would be noise in the contract and a permanent scan cost —
        replay checks every declared outcome after every step."""
        codes = {o["code"] for o in artifact["expected_outcomes"]}
        assert "DEPOSIT_BELOW_MINIMUM" not in codes

    def test_route_scope_is_stripped_from_the_artifact(self, artifact):
        """`routes` is a distillation-time concern; the schema would reject it."""
        assert all("routes" not in o for o in artifact["expected_outcomes"])

    def test_an_unscoped_outcome_is_always_attached(self):
        artifact = build(outcomes=[{
            "code": "SYSTEM_NOTICE",
            "detect": {"condition": "text_present", "value": "Scheduled maintenance"},
            "meaning": "App-wide notice, can appear anywhere.",
        }])
        assert artifact["expected_outcomes"][0]["code"] == "SYSTEM_NOTICE"

    def test_outcomes_are_not_inferred_from_the_trace(self):
        """A successful run never meets 'no such member'. Anything derived from it
        would describe only the happy path."""
        assert build(outcomes=[])["expected_outcomes"] == []

    def test_unknown_app_declares_none(self):
        assert outcomes_for("some-other-app") == []


class TestRefusals:

    def test_an_unsuccessful_run_cannot_be_distilled(self):
        """Only a verified success becomes a capability."""
        trace = [e for e in real_shaped_trace() if e.get("event") != "success_verified"]
        with pytest.raises(DistillationError, match="no verified success"):
            build(trace)

    def test_a_target_without_strategies_is_refused(self):
        trace = real_shaped_trace()
        trace[2]["target"]["strategies"] = []
        with pytest.raises(DistillationError, match="no strategies"):
            build(trace)

    def test_an_empty_trace_is_refused(self):
        with pytest.raises(DistillationError):
            build([{"event": "run_started", "goal": GOAL, "base_url": BASE},
                   {"event": "success_verified", "step": 1, "success_evidence": "x"}])

    def test_output_of_distillation_is_always_schema_checked(self):
        """The distiller cannot emit an invalid artifact even by accident."""
        trace = real_shaped_trace()
        trace[7]["output"] = "Not A Valid Name"
        artifact = build(trace)
        assert "not_a_valid_name" in artifact["outputs"]


# =============================================================================
# Redaction (§8)
# =============================================================================

class TestRedaction:

    @pytest.mark.parametrize("value,expected", [
        ("$4,523.18", "currency"), ("18240.55", "currency"), ("712", "integer"),
        ("Alice Torres", "text"), ("", "text"),
    ])
    def test_shapes(self, value, expected):
        assert shape_of(value) == expected

    def test_money_is_masked(self):
        assert mask("$4,523.18") == "$•••••"

    def test_names_survive_for_debugging(self):
        """A name in a trace is how you tell a run that read the right record from
        one that read the wrong one."""
        assert mask("Alice Torres") == "Alice Torres"


class TestSlugify:

    @pytest.mark.parametrize("text,expected", [
        ("Member number", "member_number"),
        ("Savings balance", "savings_balance"),
        ("Account  type!", "account_type"),
        ("", "value"),
    ])
    def test_names_come_from_what_the_page_calls_things(self, text, expected):
        assert slugify(text) == expected
