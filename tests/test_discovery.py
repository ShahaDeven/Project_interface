"""Tests for the discovery loop, evidence and credential handling (DESIGN §4, §8, §10).

No network and no browser: the surface and the model client are both faked, so
these run in milliseconds and can assert things a live run cannot — that a
particular sequence of tool calls produces a particular envelope, that the step
limit is really enforced, that a credential never reaches the trace.

The live run is the separate, manual thing the brief asks for. This is the harness
that makes it worth running.
"""

import copy
import json
import types

import pytest

from cua import config
from cua.agent import tools
from cua.agent.loop import DiscoveryLoop
from cua.contracts import validate_result
from cua.evidence import RunEvidence
from cua.executor.surface import Element, Observation


# ----------------------------------------------------------------- doubles --

def element(index, role="textbox", label="Member number", kind="interactive", text=""):
    return Element(index=index, kind=kind, role=role, label=label,
                   structural=f"body > table > tr:nth-of-type({index + 1}) > td > input",
                   center=(100 + index, 200 + index), box=(0, 0, 80, 20), text=text)


class FakeSurface:
    """A Surface that records what it was asked to do."""

    def __init__(self, elements=None, present=()):
        self.elements = elements or [
            element(0, "textbox", "Member number"),
            element(1, "button", "Look Up"),
            element(2, "cell", "Savings balance", kind="text", text="$4,523.18"),
            element(3, "password", "Password"),
        ]
        self.present = set(present)
        self.calls = []
        self.session_id = "browser_sess_fake"

    def observe(self, screenshot_path=None):
        if screenshot_path:
            from pathlib import Path
            Path(screenshot_path).parent.mkdir(parents=True, exist_ok=True)
            Path(screenshot_path).write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        return Observation(url="http://localhost:5000/member/12345", title="Member Profile",
                           elements=self.elements, screenshot_path=str(screenshot_path or ""),
                           app_fingerprint="legacy-cu-portal@4.2.1")

    def navigate(self, url):
        self.calls.append(("navigate", url))

    def click(self, target):
        self.calls.append(("click", target.label))

    def type(self, target, text):
        self.calls.append(("type", target.label, text))

    def read(self, target):
        self.calls.append(("read", target.label))
        return target.text or "read-value"

    def text_present(self, needle):
        return needle in self.present

    def current_url(self):
        return "http://localhost:5000/member/12345"

    def page_marker(self):
        return "Member Profile"

    def app_fingerprint(self):
        return "legacy-cu-portal@4.2.1"


def tool_use(name, **params):
    return types.SimpleNamespace(type="tool_use", id=f"toolu_{name}", name=name, input=params)


class FakeClient:
    """Replays a scripted list of tool calls, one per turn."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.messages = types.SimpleNamespace(create=self._create)

    def _create(self, **request):
        # Snapshot: the loop keeps mutating the same `messages` list after this
        # call returns, so holding a reference would let a later turn's screenshot
        # appear to have been sent on an earlier request.
        self.requests.append(copy.deepcopy(request))
        block = self.script.pop(0) if self.script else tool_use(
            "stuck", reason="script exhausted", blocker_description="x", requested_action="y")
        return types.SimpleNamespace(
            content=[block], stop_reason="tool_use",
            usage=types.SimpleNamespace(input_tokens=10, output_tokens=5))


@pytest.fixture
def evidence(tmp_path):
    return RunEvidence(run_id="run_20260814_093012", root=tmp_path)


def run_loop(script, evidence, surface=None, **kwargs):
    surface = surface or FakeSurface(present={"Member Profile"})
    client = FakeClient(script)
    loop = DiscoveryLoop(surface, client, evidence=evidence,
                         capability_name="lookup_member_balance", model="fake-model", **kwargs)
    return loop.run("look up member 12345 and read their savings balance",
                    "http://localhost:5000"), surface, client


# =============================================================================
# The action space
# =============================================================================

class TestToolDefinitions:

    def test_six_tools_exactly(self):
        assert len(tools.ALL_TOOLS) == 6
        assert {t["name"] for t in tools.ALL_TOOLS} == {
            "click", "type", "navigate", "read", "done", "stuck"}

    def test_no_wait_tool(self):
        """Waiting is executor policy: a model that can wait will wait instead of
        declaring itself stuck, and the recording captures the papering-over."""
        assert "wait" not in {t["name"] for t in tools.ALL_TOOLS}

    def test_terminal_tools_are_not_replayable_actions(self):
        """`done` and `stuck` are loop control; the artifact's step actions are
        exactly the other four."""
        assert tools.TERMINAL_TOOLS == {"done", "stuck"}
        assert tools.ACTION_TOOLS == {"click", "type", "navigate", "read"}

    def test_every_action_tool_demands_a_reason(self):
        for tool in tools.ALL_TOOLS:
            if tool["name"] in tools.ACTION_TOOLS:
                assert "reason" in tool["input_schema"]["required"], tool["name"]

    def test_done_demands_verifiable_evidence(self):
        done = next(t for t in tools.ALL_TOOLS if t["name"] == "done")
        assert set(done["input_schema"]["required"]) == {
            "summary", "outputs", "success_evidence"}


# =============================================================================
# The loop
# =============================================================================

class TestLoop:

    def test_a_successful_run_produces_a_valid_envelope(self, evidence):
        result, surface, _ = run_loop([
            tool_use("navigate", url="http://localhost:5000/login", reason="entry point"),
            tool_use("type", element_index=0, text="12345", reason="the member number"),
            tool_use("click", element_index=1, reason="run the lookup"),
            tool_use("read", element_index=2, label="savings_balance", reason="the goal value"),
            tool_use("done", summary="Read the balance.",
                     outputs={"savings_balance": "$4,523.18"},
                     success_evidence="Member Profile"),
        ], evidence)

        assert result.status == "SUCCESS"
        assert result.outputs == {"savings_balance": "$4,523.18"}
        assert validate_result(result.envelope)
        assert result.envelope["mode"] == "discovery"
        assert result.envelope["llm_call_count"] == 5
        assert ("navigate", "http://localhost:5000/login") in surface.calls

    def test_unverified_success_claim_is_rejected_until_it_is_quoted(self, evidence):
        """An agent asserting completion is a hypothesis, not a result — but a bad
        quotation is recoverable. The agent is told what was wrong and can correct
        it; the contract does not soften, since only a literal match succeeds."""
        result, surface, _ = run_loop([
            tool_use("done", summary="done", outputs={},
                     success_evidence="Savings balance $4,523.18 shown for Alice Torres"),
            tool_use("done", summary="done", outputs={"savings_balance": "$4,523.18"},
                     success_evidence="Member Profile"),
        ], evidence, surface=FakeSurface(present={"Member Profile"}))

        assert result.status == "SUCCESS"
        rejected = [e for e in evidence.read_trace()
                    if e["event"] == "success_evidence_rejected"]
        assert len(rejected) == 1
        assert "Alice Torres" in rejected[0]["claimed"]

    def test_a_claim_that_stays_unquotable_fails_the_run(self, evidence):
        """Bounded: an agent that cannot find real text after being told exactly
        what is wrong will not find it on the fourth attempt either."""
        result, _, _ = run_loop([
            tool_use("done", summary="", outputs={}, success_evidence="Definitely Finished"),
        ] * 5, evidence, surface=FakeSurface(present={"Member Profile"}))

        assert result.status == "HARD_FAILURE"
        assert "not found" in result.envelope["payload"]["observed"]

    def test_stuck_becomes_a_live_intervention_request(self, evidence):
        result, _, _ = run_loop([
            tool_use("stuck", reason="UNEXPECTED_DIALOG",
                     blocker_description="A maintenance notice covers the page.",
                     requested_action="Dismiss it, then resume."),
        ], evidence)

        assert result.status == "NEEDS_INTERVENTION"
        payload = result.envelope["payload"]
        assert payload["reason"] == "AGENT_STUCK"
        assert payload["control"] == "HUMAN"
        assert payload["session_id"] == "browser_sess_fake"

    def test_step_limit_is_enforced_by_the_loop(self, evidence):
        """The model cannot see or extend its own budget."""
        result, _, client = run_loop(
            [tool_use("click", element_index=1, reason="again")] * 10,
            evidence, max_steps=4)

        assert result.status == "HARD_FAILURE"
        assert "step limit" in result.envelope["payload"]["observed"]
        assert len(client.requests) == 4

    def test_exactly_one_tool_call_is_forced(self, evidence):
        _, _, client = run_loop([
            tool_use("done", summary="", outputs={}, success_evidence="Member Profile"),
        ], evidence)
        choice = client.requests[0]["tool_choice"]
        assert choice["type"] == "any"
        assert choice["disable_parallel_tool_use"] is True

    def test_model_that_does_not_call_a_tool_fails_cleanly(self, evidence):
        class Silent(FakeClient):
            def _create(self, **request):
                self.requests.append(request)
                return types.SimpleNamespace(
                    content=[types.SimpleNamespace(type="text", text="I think I should...")],
                    stop_reason="end_turn",
                    usage=types.SimpleNamespace(input_tokens=1, output_tokens=1))

        loop = DiscoveryLoop(FakeSurface(), Silent([]), evidence=evidence, model="fake")
        result = loop.run("goal", "http://localhost:5000")
        assert result.status == "HARD_FAILURE"
        assert "without calling a tool" in result.envelope["payload"]["observed"]

    def test_old_screenshots_are_dropped_from_context(self, evidence):
        """Every turn adds an image; by step 20 the early ones are paying rent on
        a page nobody will look at again."""
        _, _, client = run_loop(
            [tool_use("click", element_index=1, reason="step")] * 6,
            evidence, max_steps=6)

        images = 0
        for message in client.requests[-1]["messages"]:
            for block in message["content"]:
                blocks = block.get("content", [block]) if isinstance(block, dict) else []
                for item in (blocks if isinstance(blocks, list) else [blocks]):
                    if isinstance(item, dict) and item.get("type") == "image":
                        images += 1
        assert images <= 2


# =============================================================================
# Credentials (§8)
# =============================================================================

class TestSecrets:

    def test_the_token_is_substituted_at_the_keystroke(self, evidence, monkeypatch):
        monkeypatch.setenv("CUA_SECRET_OPERATOR_PASSWORD", "s3cret-value")
        result, surface, _ = run_loop([
            tool_use("type", element_index=3, text="{secrets.operator_password}",
                     reason="operator password"),
            tool_use("done", summary="", outputs={}, success_evidence="Member Profile"),
        ], evidence)

        assert ("type", "Password", "s3cret-value") in surface.calls
        assert result.status == "SUCCESS"

    def test_the_real_value_never_reaches_the_trace(self, evidence, monkeypatch):
        monkeypatch.setenv("CUA_SECRET_OPERATOR_PASSWORD", "s3cret-value")
        run_loop([
            tool_use("type", element_index=3, text="{secrets.operator_password}",
                     reason="operator password"),
            tool_use("done", summary="", outputs={}, success_evidence="Member Profile"),
        ], evidence)

        written = (evidence.dir / "trace.jsonl").read_text(encoding="utf-8")
        assert "s3cret-value" not in written
        assert "{secrets.operator_password}" in written

    def test_a_missing_secret_names_the_variable_not_the_value(self, monkeypatch):
        monkeypatch.delenv("CUA_SECRET_NOPE", raising=False)
        with pytest.raises(config.MissingCredential) as raised:
            config.secret("nope")
        assert "CUA_SECRET_NOPE" in str(raised.value)

    def test_a_real_environment_variable_beats_the_env_file(self, tmp_path, monkeypatch):
        """A shell export is a deliberate act; a file should not silently win."""
        env_file = tmp_path / ".env"
        env_file.write_text("CUA_SECRET_THING=from-file\n", encoding="utf-8")
        monkeypatch.setenv("CUA_SECRET_THING", "from-shell")
        config.load_env(env_file, force=True)
        assert config.secret("thing") == "from-shell"


# =============================================================================
# Evidence (§10)
# =============================================================================

class TestEvidence:

    def test_trace_is_line_delimited_and_survives_a_partial_run(self, evidence):
        """A run that dies mid-step still leaves a readable trace up to that
        point — which is when you most want one."""
        evidence.trace("run_started", goal="x")
        evidence.trace("step", step=1, action="navigate")
        lines = (evidence.dir / "trace.jsonl").read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2
        assert all(json.loads(line)["at"] for line in lines)

    def test_full_element_descriptor_is_recorded_not_the_index(self, evidence):
        """An index is meaningless outside the observation that produced it."""
        run_loop([
            tool_use("click", element_index=1, reason="run the lookup"),
            tool_use("done", summary="", outputs={}, success_evidence="Member Profile"),
        ], evidence)

        step = next(e for e in evidence.read_trace()
                    if e["event"] == "step" and e["action"] == "click")
        target = step["target"]
        assert target["label"] == "Look Up"
        assert target["structural"]
        assert [s["kind"] for s in target["strategies"]] == [
            "label", "structural", "coordinates"]

    def test_transcript_records_every_model_exchange(self, evidence):
        run_loop([
            tool_use("done", summary="", outputs={}, success_evidence="Member Profile"),
        ], evidence)
        lines = (evidence.dir / "transcript.jsonl").read_text(encoding="utf-8").strip().split("\n")
        directions = [json.loads(line)["direction"] for line in lines]
        assert directions == ["request", "response"]

    def test_screenshots_are_written_per_step(self, evidence):
        run_loop([
            tool_use("click", element_index=1, reason="x"),
            tool_use("done", summary="", outputs={}, success_evidence="Member Profile"),
        ], evidence)
        assert len(list(evidence.screenshots.glob("*.png"))) >= 2

    def test_result_envelope_is_written_and_valid(self, evidence):
        run_loop([
            tool_use("done", summary="", outputs={}, success_evidence="Member Profile"),
        ], evidence)
        written = json.loads((evidence.dir / "result.json").read_text(encoding="utf-8"))
        assert validate_result(written)

    def test_evidence_path_is_posix_and_relative(self, evidence):
        """This string ends up in a committed result envelope; a Windows drive
        letter has no business travelling with it."""
        assert "\\" not in evidence.path
        assert evidence.path.endswith("/")
