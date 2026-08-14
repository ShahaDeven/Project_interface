"""Trace → artifact (DESIGN §4, §5).

This is the compiler's back end: a linear recording of what one agent did once
becomes a parameterised capability that runs forever without a model. Four
transformations do the work, and each is a decision about what was *incidental*
to that run versus what was *essential* to the flow.

**Literals become inputs.** A value the agent typed is parameterisable exactly
when it came from the goal — `12345` was given, so it becomes `{inputs.*}`, while
a value the agent chose for itself is part of the flow and stays literal. That
rule is why the goal string is kept in the trace.

**Reads become typed outputs.** The trace holds each read's *shape* and a masked
form, never the figure (§8), and the shape is enough to declare a type and parse
mode. The capability contract is built without evidence ever holding a balance.

**Risk comes from configuration, not from the model.** Classified from the form
method and route the click actually submitted to, against policy.yaml.

**Checkpoints are synthesised from observed post-action state.** Never invented:
if the page after a step offered no assertable text, the step falls back to
asserting the URL rather than claiming something unverifiable.

What the distiller will not do is guess at business outcomes. A successful run
never meets "no such member", so anything inferred from the trace would describe
only the happy path. Those are declared per app in outcomes.yaml and attached
here.
"""

import re
from urllib.parse import urlparse

from ..config import SECRET_TOKEN_RE
from ..contracts import validate_artifact
from ..policy import RiskPolicy

ACTIONS = ("navigate", "click", "type", "read")


class DistillationError(ValueError):
    """The trace cannot become a valid capability."""


def slugify(text, fallback="value"):
    """'Member number' -> 'member_number'. Names in the contract are derived from
    what the page calls things, so a reviewer can match artifact to screen."""
    slug = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    slug = re.sub(r"_+", "_", slug)
    if not slug or not slug[0].isalpha():
        slug = fallback if not slug else f"{fallback}_{slug}"
    return slug


def _goal_literals(goal):
    """Tokens in the goal that could plausibly be a parameter.

    Numbers and quoted strings only. Deliberately narrow: templating a value that
    merely happens to appear in the goal's prose would produce a capability with a
    parameter nobody meant to expose.
    """
    # Decimals count as one token: a goal saying "a deposit of 150.00" must yield
    # the literal "150.00", not "150" and "00", or the value the agent typed will
    # match neither and silently stay hardcoded in the artifact.
    literals = set(re.findall(r"\b\d{2,}(?:\.\d+)?\b", goal or ""))
    for match in re.finditer(r"['\"]([^'\"]{2,40})['\"]", goal or ""):
        literals.add(match.group(1))
    return literals


def _input_declaration(value, label):
    """Type and validate an input from the single example we observed.

    A pattern derived from one sample is a heuristic, and an honest one: five
    digits in, `^[0-9]{5}$` out. It fails closed — a wrong pattern rejects a valid
    input at the CLI before a browser opens, which is a cheap and obvious failure,
    rather than half-completing a flow.
    """
    declaration = {
        "type": "string",
        "required": True,
        "description": f"Value for the '{label}' field.",
    }
    if value.isdigit():
        declaration["pattern"] = f"^[0-9]{{{len(value)}}}$"
    return declaration


def _output_declaration(shape, label, step_id):
    kinds = {
        "currency": ("number", "currency"),
        "number": ("number", "number"),
        "integer": ("integer", "integer"),
        "text": ("string", "text"),
    }
    kind, parse = kinds.get(shape, ("string", "text"))
    return {
        "type": kind,
        "description": f"Value shown for '{label}'.",
        "source_step": step_id,
        "parse": parse,
    }


def _checkpoint(after, templated_url, base_url):
    """Assert that the step worked, using what was actually observed.

    Prefers page text: a URL can be right while the page behind it is an error.
    Falls back to the URL path — templated, so a checkpoint on member 12345's
    profile still passes for member 23456.
    """
    marker = (after or {}).get("marker")
    if marker:
        return {"condition": "text_present", "value": marker}

    observed = (after or {}).get("url") or templated_url or base_url
    path = urlparse(observed.replace("{base_url}", base_url)).path or "/"
    return {"condition": "url_matches", "value": path}


def distil(trace, capability_name, app, app_fingerprint, recorded_at, run_id,
           outcomes=(), risk_policy=None, version="1.0.0"):
    """Build a capability artifact from a completed discovery trace."""
    risk_policy = risk_policy or RiskPolicy()

    started = next((e for e in trace if e.get("event") == "run_started"), None)
    if started is None:
        raise DistillationError("trace has no run_started event")
    goal = started.get("goal", "")
    base_url = (started.get("base_url") or "").rstrip("/")

    verified = next((e for e in trace if e.get("event") == "success_verified"), None)
    if verified is None:
        raise DistillationError(
            "trace has no verified success; only a successful run can be distilled")

    recorded = [e for e in trace if e.get("event") == "step" and e.get("action") in ACTIONS]
    if not recorded:
        raise DistillationError("trace contains no replayable steps")

    literals = _goal_literals(goal)
    inputs, outputs, steps = {}, {}, []
    by_literal = {}

    for position, event in enumerate(recorded, start=1):
        action = event["action"]
        target = event.get("target") or {}
        step = {"id": position, "action": action,
                "risk": risk_policy.classify(action, target.get("form"))}
        if event.get("reason"):
            step["reason"] = event["reason"]

        if action == "navigate":
            url = event.get("url", "")
            step["url"] = url.replace(base_url, "{base_url}") if base_url else url
            step["checkpoint"] = _checkpoint(event.get("after"), step["url"], base_url)

        else:
            step["target"] = _target(target)

            if action == "click":
                step["checkpoint"] = _checkpoint(event.get("after"), None, base_url)

            elif action == "type":
                value = event.get("value", "")
                label = target.get("label") or "value"
                if SECRET_TOKEN_RE.search(value):
                    step["value"] = value                      # already a token
                elif value in literals:
                    name = by_literal.get(value) or slugify(label, "input")
                    by_literal[value] = name
                    inputs.setdefault(name, _input_declaration(value, label))
                    step["value"] = f"{{inputs.{name}}}"
                else:
                    step["value"] = value                      # incidental to the flow

            elif action == "read":
                name = slugify(event.get("output") or target.get("label"), "output")
                step["output"] = name
                outputs[name] = _output_declaration(
                    event.get("value_shape", "text"), target.get("label") or name, position)

        steps.append(step)

    secrets = sorted({name for step in steps
                      for name in SECRET_TOKEN_RE.findall(step.get("value", "") or "")})

    # Once a literal has become a parameter, it must stop appearing as a literal
    # anywhere in the contract. A description reading "look up member 12345" tells
    # a calling agent this capability looks up member 12345 — which is exactly what
    # it no longer does.
    for step in steps:
        if step.get("reason"):
            step["reason"] = _generalise(step["reason"], by_literal)
        # Checkpoints included: a URL fallback recorded as "/member/12345" is the
        # same defect as a success checkpoint quoting a balance — it asserts
        # something true only of the run that recorded it.
        if step.get("checkpoint"):
            step["checkpoint"]["value"] = _generalise(step["checkpoint"]["value"], by_literal)

    capability = {
        "name": capability_name,
        "version": version,
        "description": _describe(_generalise(goal, by_literal), outputs),
        "recorded_against": {
            "app": app,
            "app_fingerprint": app_fingerprint,
            "recorded_at": recorded_at,
            "discovery_run_id": run_id,
        },
    }
    if secrets:
        capability["requires_secrets"] = secrets

    artifact = {
        "schema_version": "1.0",
        "capability": capability,
        "inputs": inputs,
        "outputs": outputs,
        "expected_outcomes": _applicable_outcomes(outcomes, _visited_paths(recorded)),
        "steps": steps,
        "success": {"checkpoint": {"condition": "text_present",
                                   "value": verified["success_evidence"]}},
    }
    return validate_artifact(artifact)


def _target(target):
    """Carry the whole strategy chain, plus the frame the element lived in."""
    if not target.get("strategies"):
        raise DistillationError(
            f"step targets {target.get('label')!r} but recorded no strategies")
    out = {"strategies": target["strategies"]}
    if target.get("frame"):
        out["frame"] = target["frame"]
    return out


def _visited_paths(recorded):
    """Every route this flow actually touched, from the URLs observed at run time."""
    paths = set()
    for event in recorded:
        after = event.get("after") or {}
        for url in (event.get("url"), after.get("url")):
            if url:
                paths.add(urlparse(url).path or "/")
    return paths


def _applicable_outcomes(outcomes, visited):
    """Attach only the declared outcomes this flow could actually produce.

    An outcome scoped to routes the capability never visits is noise in a contract
    meant to be reviewable, and worse at run time: replay scans for every declared
    outcome after every step, so an unreachable one is a permanent cost and a
    standing false-positive surface. Unscoped outcomes are app-wide and always kept.

    `routes` is a distillation-time concern and is stripped before it reaches the
    artifact, which the schema would reject it from anyway.
    """
    applicable = []
    for outcome in outcomes:
        scope = outcome.get("routes")
        if scope and not any(re.match(pattern, path)
                             for pattern in scope for path in visited):
            continue
        applicable.append({key: value for key, value in outcome.items() if key != "routes"})
    return applicable


def _generalise(text, by_literal):
    """Replace recorded literals with the parameters they became.

    Longest first, so a literal that contains another does not get half-replaced.
    """
    for literal in sorted(by_literal, key=len, reverse=True):
        text = text.replace(literal, "{inputs.%s}" % by_literal[literal])
    return text


def _describe(goal, outputs):
    names = ", ".join(outputs) if outputs else "no values"
    return f"Recorded from the goal: {goal}. Returns {names}."
