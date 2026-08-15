"""The discovery loop: observe → decide → act (DESIGN §4).

Hand-rolled, ~200 lines including bookkeeping. A framework would earn its place
here if there were multiple concurrent capabilities or durable multi-agent state;
there is one linear agent, and the loop structure itself is part of what is being
evaluated, so hiding it inside someone else's abstraction would be hiding the
work.

Three things this file is careful about:

**Stopping conditions belong to the loop.** Max steps and wall clock are enforced
here and are not visible to the model, because a model that can extend its own
budget will.

**The agent's claim of success is checked.** `done` carries `success_evidence`,
and the run is only recorded as a success if that text is actually on the page. An
agent asserting completion is a hypothesis, not a result.

**Old screenshots are dropped from the context.** Every turn adds an image, and by
step 20 the earliest ones are paying rent on a page nobody will look at again. The
most recent two are kept; older ones become a one-line placeholder.
"""

import base64
import time
import traceback
from dataclasses import dataclass, field

from .. import config
from ..contracts import validate_result
from ..evidence import RunEvidence, utc_now
from ..executor import TargetNotFound
from ..hitl import RESUME, Handoff, InterventionRequest
from ..policy import PolicyViolation, mask, shape_of
from . import tools
from .prompts import SYSTEM_PROMPT, opening_message

KEEP_IMAGES = 2

# How many times the agent may re-quote its success evidence before the run is
# failed. Bounded, because an agent that cannot find real text on the page after
# being told exactly what is wrong is not going to on the fourth try either.
MAX_EVIDENCE_CORRECTIONS = 2


@dataclass
class DiscoveryResult:
    status: str
    run_id: str
    evidence_path: str
    steps: list = field(default_factory=list)
    outputs: dict = field(default_factory=dict)
    summary: str = ""
    llm_call_count: int = 0
    envelope: dict = field(default_factory=dict)

    @property
    def succeeded(self):
        return self.status == "SUCCESS"


class DiscoveryLoop:

    def __init__(self, surface, client, evidence=None, capability_name="discovered",
                 model=None, max_steps=None, wall_clock_seconds=None, console=None):
        self.surface = surface
        self.client = client
        self.evidence = evidence or RunEvidence()
        # The same seam replay uses (§9). `stuck` is a trigger into
        # PAUSED_FOR_HUMAN exactly like an unknown dialog is, and the console
        # being an interface is what lets one handoff path serve both callers.
        self.handoff = Handoff(surface, self.evidence, console)
        self.capability_name = capability_name
        self.model = model or config.model()
        self.max_steps = max_steps or config.MAX_STEPS
        self.wall_clock = wall_clock_seconds or config.WALL_CLOCK_SECONDS
        self.llm_call_count = 0
        self.evidence_rejections = 0
        self.steps = []
        self.goal = ""
        # Values as read, kept in memory only: they reach the result envelope,
        # which is caller-bound, and never the step trace.
        self.outputs = {}
        # Everything observed to depend on this run's inputs — values read from the
        # page, and values typed that came from the goal. A success checkpoint may
        # not contain any of them.
        self.varying_values = set()

    # ------------------------------------------------------------- helpers --

    def _image_block(self, path):
        with open(path, "rb") as handle:
            data = base64.b64encode(handle.read()).decode("ascii")
        return {"type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": data}}

    def _observe(self, name):
        path = self.evidence.screenshot_path(name)
        return self.surface.observe(screenshot_path=path), path

    def _observation_blocks(self, observation, screenshot):
        return [{"type": "text", "text": observation.render()}, self._image_block(screenshot)]

    @staticmethod
    def _prune_images(messages, keep=KEEP_IMAGES):
        """Drop all but the most recent `keep` screenshots from the context.

        Images dominate token cost in a loop like this and stale ones have no
        decision value — the agent acts on the current page. Replaced rather than
        deleted so the conversation still reads coherently.

        Screenshots live at two depths: directly in a message's content (the
        opening message) and nested inside a tool_result (every turn after). Both
        are walked, and each list is mutated in place — an earlier version built a
        throwaway list for the first case, so the opening screenshot was counted
        but never actually dropped.
        """
        placeholder = {"type": "text", "text": "[screenshot omitted to save context]"}
        seen = 0

        def take(container, index):
            nonlocal seen
            seen += 1
            if seen > keep:
                container[index] = dict(placeholder)

        for message in reversed(messages):
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for index in reversed(range(len(content))):
                block = content[index]
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "image":
                    take(content, index)
                    continue
                inner = block.get("content")
                if isinstance(inner, list):
                    for position in reversed(range(len(inner))):
                        item = inner[position]
                        if isinstance(item, dict) and item.get("type") == "image":
                            take(inner, position)
        return messages

    def _call_model(self, messages):
        self._prune_images(messages)
        request = {
            "model": self.model,
            "max_tokens": config.MAX_TOKENS,
            "system": SYSTEM_PROMPT,
            "tools": tools.ALL_TOOLS,
            # Exactly one tool call per turn (§4): forced, and not parallel.
            "tool_choice": {"type": "any", "disable_parallel_tool_use": True},
            "messages": messages,
        }
        self.evidence.transcript("request", {
            "model": self.model,
            "message_count": len(messages),
            "last_message": _summarise(messages[-1]),
        })
        response = self.client.messages.create(**request)
        self.llm_call_count += 1
        self.evidence.transcript("response", {
            "stop_reason": response.stop_reason,
            "content": [_block_summary(block) for block in response.content],
            "usage": {"input_tokens": getattr(response.usage, "input_tokens", None),
                      "output_tokens": getattr(response.usage, "output_tokens", None)},
        })
        return response

    # ---------------------------------------------------------------- run --

    def run(self, goal, base_url):
        started = time.monotonic()
        started_at = utc_now()
        self.goal = goal
        self.evidence.trace("run_started", mode="discovery", goal=goal,
                            base_url=base_url, model=self.model)

        observation, screenshot = self._observe("step00_start")
        messages = [{"role": "user", "content": [
            {"type": "text", "text": opening_message(goal, base_url)},
            *self._observation_blocks(observation, screenshot)[1:],
        ]}]

        outcome = None
        for step_number in range(1, self.max_steps + 1):
            if time.monotonic() - started > self.wall_clock:
                outcome = self._fail(step_number, "wall clock",
                                     f"under {self.wall_clock}s", "time limit reached")
                break

            response = self._call_model(messages)
            call = _first_tool_use(response)
            if call is None:
                outcome = self._fail(step_number, "none",
                                     "a tool call", "model replied without calling a tool")
                break

            messages.append({"role": "assistant", "content": _to_blocks(response.content)})

            try:
                outcome, correction = self._dispatch(call, step_number, observation)
            except PolicyViolation as violation:
                outcome = self._fail(step_number, call.name,
                                     "an action inside the allowlist", str(violation))
                break
            except TargetNotFound as missing:
                outcome = self._fail(step_number, call.name,
                                     "a resolvable target", str(missing))
                break
            except Exception as error:  # noqa: BLE001 - see below
                # Anything the surface throws becomes a HARD_FAILURE with
                # forensics, never a traceback out of the CLI. A run that dies
                # ungracefully leaves no result envelope, so the caller cannot tell
                # a crashed automation from one that never started — and §7 exists
                # precisely so every run answers that question. The traceback is
                # kept in the evidence directory rather than discarded.
                self.evidence.write_text(
                    "failure_traceback.txt", traceback.format_exc())
                outcome = self._fail(step_number, call.name, "the action to complete",
                                     f"{type(error).__name__}: {error}")
                break

            if outcome is not None and outcome.get("kind") == "stuck":
                # An agent that declares itself stuck is §9's discovery trigger.
                # If a human unblocks the page, the run continues rather than
                # ending — the model gets a fresh observation and carries on from
                # a situation it could not resolve alone.
                unblocked = self._offer_stuck(outcome, screenshot)
                if unblocked:
                    outcome, correction = None, unblocked

            if outcome is not None:
                break

            observation, screenshot = self._observe(f"step{step_number:02d}")
            blocks = self._observation_blocks(observation, screenshot)
            if correction:
                blocks = [{"type": "text", "text": correction}] + blocks
            messages.append({"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": call.id,
                "content": blocks,
            }]})
        else:
            outcome = self._fail(self.max_steps, "none",
                                 f"completion within {self.max_steps} steps",
                                 "step limit reached")

        envelope = self._envelope(outcome, started_at)
        self.evidence.write_json("result.json", envelope)
        self.evidence.trace("run_finished", status=envelope["status"],
                            llm_call_count=self.llm_call_count)
        return DiscoveryResult(
            status=envelope["status"], run_id=self.evidence.run_id,
            evidence_path=self.evidence.path, steps=self.steps,
            outputs=outcome.get("outputs", {}), summary=outcome.get("summary", ""),
            llm_call_count=self.llm_call_count, envelope=envelope)

    # ----------------------------------------------------------- dispatch --

    def _dispatch(self, call, step_number, observation):
        """Execute one tool call. Returns (terminal_outcome_or_None, result_blocks)."""
        name, params = call.name, call.input

        if name == "navigate":
            self.surface.navigate(params["url"])
            self._record(step_number, "navigate", params, url=params["url"],
                         after=self._after())
            return None, None

        if name in ("click", "type", "read"):
            element = observation.by_index(params["element_index"])

            if name == "click":
                self.surface.click(element)
                self._record(step_number, "click", params, element=element,
                             after=self._after())
                return None, None

            if name == "type":
                typed = params["text"]
                # The substitution happens here and nowhere earlier: the trace
                # below records `typed`, which still holds the token.
                self.surface.type(element, config.resolve_secrets(typed))
                if not config.secret_names_in(typed) and typed and typed in self.goal:
                    # Came from the goal, so it is a parameter of this capability
                    # and will differ on the next invocation.
                    self.varying_values.add(typed)
                self._record(step_number, "type", params, element=element, value=typed)
                return None, None

            value = self.surface.read(element)
            self.outputs[params["label"]] = value
            if value:
                self.varying_values.add(value)
            # The figure itself goes to the result envelope, which is caller-bound.
            # The trace gets its shape and a masked form (§8) — enough to type the
            # output in the artifact, without committing a balance to evidence.
            self._record(step_number, "read", params, element=element,
                         output=params["label"], value_shape=shape_of(value),
                         value_masked=mask(value))
            return None, None

        if name == "done":
            evidence_text = params["success_evidence"]
            problem = self._evidence_problem(evidence_text)
            verified = problem is None
            self._record(step_number, "done", params, verified=verified)

            if not verified:
                # An agent asserting success is a hypothesis, not a result — but a
                # rejected claim is a recoverable condition, not a terminal one.
                # The observed failure mode is a model that *did the task
                # correctly* and then described the evidence in prose instead of
                # quoting it. Discarding a correct run over its last field wastes
                # every model call that produced it, so the agent is told what was
                # wrong and gets a bounded number of attempts to quote real text.
                # The contract does not soften: the run still only succeeds on a
                # literal match.
                self.evidence_rejections += 1
                self.evidence.trace("success_evidence_rejected", step=step_number,
                                    claimed=evidence_text, why=problem["why"],
                                    attempt=self.evidence_rejections)
                if self.evidence_rejections > MAX_EVIDENCE_CORRECTIONS:
                    return self._fail(step_number, "read",
                                      f"a stable success checkpoint, verified on the page",
                                      problem["observed"]), None
                return None, problem["correction"]

            # success_evidence is recorded because it becomes the artifact's
            # success checkpoint; the agent's own `outputs` are not, since the
            # values it reports are the ones already read step by step and they
            # belong in the envelope, not duplicated into evidence.
            self.evidence.trace("success_verified", step=step_number,
                                success_evidence=evidence_text)
            return {"kind": "done", "outputs": self.outputs or params.get("outputs", {}),
                    "summary": params.get("summary", ""),
                    "success_evidence": evidence_text, "step": step_number}, None

        if name == "stuck":
            self._record(step_number, "stuck", params)
            return {"kind": "stuck", "step": step_number, "params": params}, None

        raise ValueError(f"unknown tool {name!r}")

    def _evidence_problem(self, text):
        """Why this success evidence cannot be the capability's checkpoint, if so.

        Two ways to fail, and the second is the one that bites silently.

        *Not present* — the agent described the page instead of quoting it. Caught
        by looking at the page.

        *Varies with the input* — the agent quoted something real, but quoted a
        **value**: the balance it just read, or the member number it was given. That
        passes a presence check perfectly and produces a capability that succeeds
        only for the run that recorded it, failing for every other input with no
        obvious cause. So any text containing a value this run read or was handed is
        refused, whatever the page says.

        A checkpoint must be true for every valid input, not just this one.
        """
        normalised = " ".join((text or "").split())
        for value in self.varying_values:
            varying = " ".join(value.split())
            if varying and varying in normalised:
                return {
                    "why": "varies_with_input",
                    "observed": (f"the claimed evidence contains {varying!r}, which is a "
                                 f"value this run read or was given"),
                    "correction": (
                        f"Rejected. Your success_evidence contains {varying!r}:\n"
                        f"  {text!r}\n"
                        f"That is a VALUE — it belongs to this particular run and would be "
                        f"false for any other input, so it cannot be the capability's "
                        f"permanent checkpoint. Call `done` again quoting text that names "
                        f"the page or end state you reached and would be on screen "
                        f"whatever the inputs were."),
                }

        if not self.surface.text_present(text):
            return {
                "why": "not_present",
                "observed": "the claimed evidence was not found on the page",
                "correction": (
                    f"Rejected. The success_evidence you gave is not on the page:\n"
                    f"  {text!r}\n"
                    f"That is a description, not a quotation. Call `done` again with a "
                    f"SHORT contiguous phrase copied character-for-character from the "
                    f"element list below, naming the page or state you ended on."),
            }
        return None

    def _after(self):
        """State immediately after a page-transitioning action.

        The distiller needs this to synthesise a per-step checkpoint: without it,
        a recorded navigate or click has nothing to assert about having worked, and
        the schema requires exactly that. `marker` is None when the page offers no
        text worth asserting, so the distiller can fall back rather than invent one.
        """
        return {"url": self.surface.current_url(), "marker": self.surface.page_marker()}

    def _record(self, step_number, action, params, element=None, **extra):
        """Append to the step trace.

        The full element descriptor is recorded, not the index: an index is
        meaningless outside the observation that produced it, and the descriptor
        is what the distiller turns into a strategy chain.
        """
        entry = {"step": step_number, "action": action,
                 "reason": params.get("reason", ""), **extra}
        if element is not None:
            entry["target"] = {
                "role": element.role, "label": element.label, "kind": element.kind,
                "structural": element.structural, "center": list(element.center),
                "box": list(element.box), "frame": element.frame,
                "form": element.form,
                "strategies": element.as_strategies(),
            }
        self.steps.append(entry)
        self.evidence.trace("step", **entry)
        return entry

    def _fail(self, step_number, action, expected, observed):
        self.evidence.trace("hard_failure", step=step_number, action_attempted=action,
                            expected=expected, observed=observed)
        return {"kind": "failure", "step": step_number, "action": action,
                "expected": expected, "observed": observed}

    # ----------------------------------------------------------- envelope --

    def _offer_stuck(self, outcome, screenshot):
        """Hand a blocked agent to a human. Returns what to tell the model, or None.

        The screenshot is the one already taken of the page the agent was looking
        at when it gave up, rather than a fresh capture: it is the image that
        matches the blocker being described.
        """
        params = outcome["params"]
        step = outcome["step"]
        request = InterventionRequest(
            run_id=self.evidence.run_id,
            capability=self.capability_name,
            step=step,
            steps_total=self.max_steps,
            reason="AGENT_STUCK",
            detail=params["blocker_description"],
            requested_action=params["requested_action"],
            screenshot=str(screenshot or ""),
            # The agent is stuck on the page it is already on, so the human acts
            # there and the model then re-observes. Nothing is re-sent.
            when="after_step",
        )
        if self.handoff.offer(request) != RESUME:
            return None
        self.handoff.resumed(step)

        did = (self.handoff.pauses[-1]["human_actions"] or [{}])[0].get(
            "summary", "no observable change")
        # `resumed`, not `verified`: a flow still being discovered has no
        # checkpoint to re-check, and the record must not claim one was.
        self.handoff.resolve(step, "resumed")
        return (f"A human operator took control at this point and acted on the page: "
                f"{did}. Control is back with you. Look at the current observation "
                f"before deciding anything — it may no longer match what blocked you.")

    def _envelope(self, outcome, started_at):
        kind = outcome.get("kind")
        common = {
            "run_id": self.evidence.run_id,
            "capability": self.capability_name,
            "capability_version": "1.0.0",
            "mode": "discovery",
            "inputs": {},
            "started_at": started_at,
            "ended_at": utc_now(),
            "steps_completed": len(self.steps),
            "steps_total": len(self.steps),
            "llm_call_count": self.llm_call_count,
            "evidence_path": self.evidence.path,
        }
        fingerprint = self.surface.app_fingerprint()
        if fingerprint:
            common["app_fingerprint_observed"] = fingerprint
        if self.handoff.pauses:
            common["intervention_record"] = self.handoff.record()

        if kind == "done":
            common.update(status="SUCCESS", payload={
                "outputs": outcome["outputs"], "checkpoint_verified": True})
        elif kind == "stuck":
            params = outcome["params"]
            common.update(status="NEEDS_INTERVENTION", payload={
                "reason": "AGENT_STUCK",
                "detail": params["blocker_description"],
                "paused_at_step": outcome["step"],
                "session_id": self.surface.session_id,
                "requested_action": params["requested_action"],
                "control": "HUMAN",
            })
        else:
            common.update(status="HARD_FAILURE", payload={
                "failed_at_step": max(outcome["step"], 1),
                "action_attempted": outcome["action"] if outcome["action"] in
                ("navigate", "click", "type", "read") else "read",
                "expected": outcome["expected"],
                "observed": outcome["observed"],
            })
        return validate_result(common)


# ------------------------------------------------------------- SDK helpers --

def _first_tool_use(response):
    for block in response.content:
        if getattr(block, "type", None) == "tool_use":
            return block
    return None


def _to_blocks(content):
    """Assistant content, as plain dicts for the next request."""
    blocks = []
    for block in content:
        kind = getattr(block, "type", None)
        if kind == "text":
            blocks.append({"type": "text", "text": block.text})
        elif kind == "tool_use":
            blocks.append({"type": "tool_use", "id": block.id,
                           "name": block.name, "input": block.input})
    return blocks


def _block_summary(block):
    kind = getattr(block, "type", None)
    if kind == "tool_use":
        return {"type": "tool_use", "name": block.name, "input": block.input}
    if kind == "text":
        return {"type": "text", "text": block.text}
    return {"type": kind}


def _summarise(message):
    """Transcript-safe view of a message: images become a marker, text is kept."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    out = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "image":
            out.append({"type": "image", "note": "screenshot"})
        elif block.get("type") == "tool_result":
            out.append({"type": "tool_result",
                        "content": [_summarise({"content": block.get("content", [])})]})
        else:
            out.append(block)
    return out
