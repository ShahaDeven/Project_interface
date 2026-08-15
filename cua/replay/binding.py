"""Binding a capability's inputs, and resolving them into recorded steps.

All of this runs *before* the browser opens. An artifact declares its inputs with
types and patterns precisely so a bad member number is rejected in milliseconds
rather than three steps into a flow, and a mutating capability never gets halfway
through on parameters that were never going to work. Failing cheaply is the whole
reason the contract carries validation at all.

Problems are aggregated, for the same reason contract errors are: fixing an
invocation one error per run is miserable.
"""

import re

from .. import config

TEMPLATE_RE = re.compile(
    r"\{(base_url|inputs\.[a-z][a-z0-9_]*|secrets\.[a-z][a-z0-9_]*)\}")

CURRENCY_STRIP = re.compile(r"[^\d.\-]")


class InputError(ValueError):
    """The supplied parameters cannot satisfy this capability's contract."""

    def __init__(self, problems):
        self.problems = list(problems)
        detail = "\n".join("  - " + problem for problem in self.problems)
        super().__init__(f"invalid parameters ({len(self.problems)}):\n{detail}")


class ParseError(ValueError):
    """An extracted value does not match the type its contract declares."""


def parse_params(pairs):
    """`--param name=value` into a dict, without swallowing '=' in the value."""
    params = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise InputError([f"{pair!r} is not name=value"])
        name, _, value = pair.partition("=")
        params[name.strip()] = value
    return params


def _coerce(raw, declared_type, name, problems):
    try:
        if declared_type == "integer":
            return int(str(raw).strip())
        if declared_type == "number":
            return float(str(raw).strip())
        if declared_type == "boolean":
            text = str(raw).strip().lower()
            if text in ("true", "yes", "1"):
                return True
            if text in ("false", "no", "0"):
                return False
            raise ValueError(text)
        return str(raw)
    except (TypeError, ValueError):
        problems.append(f"{name}: {raw!r} is not a valid {declared_type}")
        return None


def bind_inputs(artifact, params):
    """Validate supplied parameters against the artifact's declared inputs."""
    declared = artifact.get("inputs", {})
    problems = []
    values = {}

    unknown = sorted(set(params) - set(declared))
    if unknown:
        problems.append(
            f"unknown parameter(s) {unknown}; this capability declares "
            f"{sorted(declared) or 'none'}")

    for name, spec in declared.items():
        if name in params:
            raw = params[name]
        elif "default" in spec:
            raw = spec["default"]
        elif spec.get("required"):
            problems.append(f"{name}: required ({spec.get('description', '')})".rstrip(" ("))
            continue
        else:
            continue

        pattern = spec.get("pattern")
        if pattern and not re.match(pattern, str(raw)):
            problems.append(f"{name}: {raw!r} does not match {pattern}")
            continue

        if spec.get("enum") and raw not in spec["enum"]:
            problems.append(f"{name}: {raw!r} is not one of {spec['enum']}")
            continue

        coerced = _coerce(raw, spec.get("type", "string"), name, problems)
        if coerced is not None:
            values[name] = coerced

    if problems:
        raise InputError(problems)
    return values


def resolve(text, inputs, base_url):
    """Substitute {base_url}, {inputs.*} and {secrets.*} into a recorded string.

    Secrets are fetched at the point of substitution, so a credential exists in
    memory only for as long as the action that uses it — and never in the artifact
    that referenced it.
    """
    if not text:
        return text

    def substitute(match):
        token = match.group(1)
        if token == "base_url":
            return base_url.rstrip("/")
        namespace, name = token.split(".", 1)
        if namespace == "inputs":
            if name not in inputs:
                raise InputError([f"{{inputs.{name}}} is referenced but was not supplied"])
            return str(inputs[name])
        return config.secret(name)

    return TEMPLATE_RE.sub(substitute, text)


def parse_value(raw, mode="text"):
    """Coerce an observed string into the type its output declaration promises.

    A caller receiving `savings_balance` expects a number it can compare, not the
    string "$4,523.18" — the artifact says how to read the page, and this is where
    that instruction is honoured.
    """
    text = (raw or "").strip()
    if mode == "text":
        return text
    cleaned = CURRENCY_STRIP.sub("", text)
    if not cleaned or cleaned in ("-", ".", "-."):
        raise ParseError(f"cannot read {text!r} as {mode}")
    try:
        return int(cleaned) if mode == "integer" else float(cleaned)
    except ValueError:
        raise ParseError(f"cannot read {text!r} as {mode}") from None
