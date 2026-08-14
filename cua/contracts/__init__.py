"""Contract loading and validation (DESIGN §5, §7).

Two layers, deliberately:

* **JSON Schema** owns *shape* — which fields exist, their types, and the
  action-specific rules that make a half-formed step impossible (a navigate with
  no checkpoint, a coordinates strategy with nothing to verify against).
* **This module** owns *referential integrity* — the cross-field facts JSON Schema
  cannot express. That an output's `source_step` names a read step that actually
  fills it. That `{inputs.member_id}` refers to an input that was declared. That
  step ids are contiguous.

Both run on save and on load. Validating on load matters more than it sounds: an
artifact is executed against a real system, so a file that has been hand-edited
into an inconsistent state should fail before the browser opens, not halfway
through a mutation.

Errors are aggregated rather than raised one at a time — fixing an artifact one
error per run is miserable.
"""

import json
import re
from datetime import datetime
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

# jsonschema treats `format` as an annotation unless a checker is registered, and
# its built-in date-time check is a no-op without an optional RFC-3339 package. A
# timestamp that silently accepts "last Tuesday" is worse than no timestamp, and
# the stdlib already parses ISO 8601 — so the check is registered here rather than
# bought as a dependency. Scoped to this instance; no global state touched.
FORMATS = FormatChecker()


@FORMATS.checks("date-time", raises=ValueError)
def _is_iso_datetime(value):
    if not isinstance(value, str):
        return True
    datetime.fromisoformat(value)
    return True

SCHEMA_DIR = Path(__file__).parent
ARTIFACT_SCHEMA_PATH = SCHEMA_DIR / "artifact.schema.json"
RESULT_SCHEMA_PATH = SCHEMA_DIR / "result.schema.json"

# {base_url}, {inputs.member_id}, {secrets.operator_password}
TEMPLATE_RE = re.compile(r"\{([a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)?)\}")
BARE_TEMPLATES = {"base_url"}
TEMPLATE_NAMESPACES = {"inputs", "secrets"}


class ContractError(ValueError):
    """One or more contract violations. Carries every problem found, not the first."""

    def __init__(self, what, problems):
        self.problems = list(problems)
        detail = "\n".join("  - " + p for p in self.problems)
        super().__init__(f"{what} failed validation ({len(self.problems)} problem(s)):\n{detail}")


def _load_schema(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _schema_problems(document, schema_path):
    """Shape errors, sorted by document position so output is stable."""
    validator = Draft202012Validator(_load_schema(schema_path), format_checker=FORMATS)
    problems = []
    for error in sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path)):
        location = "/".join(str(part) for part in error.absolute_path) or "(root)"
        problems.append(f"{location}: {error.message}")
    return problems


def _templates_in(text):
    return TEMPLATE_RE.findall(text or "")


def _template_problems(where, text, declared_inputs, declared_secrets):
    problems = []
    for token in _templates_in(text):
        if "." not in token:
            if token not in BARE_TEMPLATES:
                problems.append(
                    f"{where}: unknown template {{{token}}} "
                    f"(expected one of {sorted(BARE_TEMPLATES)} or a namespaced reference)")
            continue
        namespace, name = token.split(".", 1)
        if namespace not in TEMPLATE_NAMESPACES:
            problems.append(f"{where}: unknown template namespace '{namespace}' in {{{token}}}")
        elif namespace == "inputs" and name not in declared_inputs:
            problems.append(f"{where}: {{{token}}} refers to an input that is not declared")
        elif namespace == "secrets" and name not in declared_secrets:
            problems.append(
                f"{where}: {{{token}}} is not listed in capability.requires_secrets — "
                f"a capability must declare what credentials it needs")
    return problems


def _artifact_integrity_problems(artifact):
    """Cross-field checks. Assumes the document already passed the schema."""
    problems = []
    steps = artifact.get("steps", [])
    declared_inputs = set(artifact.get("inputs", {}))
    declared_secrets = set(artifact.get("capability", {}).get("requires_secrets", []))

    # Step ids are the addressing scheme for outputs, failures and interventions:
    # if they are not 1..N in order, "failed_at_step: 4" is ambiguous.
    expected_ids = list(range(1, len(steps) + 1))
    actual_ids = [step.get("id") for step in steps]
    if actual_ids != expected_ids:
        problems.append(f"steps: ids must be contiguous from 1 and in order, got {actual_ids}")

    # Every output is filled by exactly one read step, and says which.
    reads = {}
    for step in steps:
        if step.get("action") == "read":
            name = step.get("output")
            reads.setdefault(name, []).append(step.get("id"))

    for name, declaration in artifact.get("outputs", {}).items():
        producing = reads.get(name, [])
        if not producing:
            problems.append(f"outputs/{name}: declared but no read step fills it")
        elif len(producing) > 1:
            problems.append(f"outputs/{name}: filled by more than one read step {producing}")
        elif declaration.get("source_step") != producing[0]:
            problems.append(
                f"outputs/{name}: source_step is {declaration.get('source_step')} "
                f"but the read step that fills it is {producing[0]}")

    for name, ids in reads.items():
        if name not in artifact.get("outputs", {}):
            problems.append(f"steps/{ids[0]}: reads into '{name}', which is not a declared output")

    # Templates resolve against something the caller actually supplies.
    for step in steps:
        where = f"steps/{step.get('id')}"
        problems += _template_problems(where, step.get("url"), declared_inputs, declared_secrets)
        problems += _template_problems(where, step.get("value"), declared_inputs, declared_secrets)

    # Outcome codes are what the caller branches on, so they must be unambiguous.
    codes = [outcome.get("code") for outcome in artifact.get("expected_outcomes", [])]
    duplicates = sorted({code for code in codes if codes.count(code) > 1})
    if duplicates:
        problems.append(f"expected_outcomes: duplicate codes {duplicates}")

    return problems


def validate_artifact(artifact):
    """Raise ContractError if the artifact is not executable. Returns it otherwise."""
    problems = _schema_problems(artifact, ARTIFACT_SCHEMA_PATH)
    if not problems:
        # Integrity checks assume a well-shaped document; running them on a
        # schema-invalid one produces noise that buries the real error.
        problems = _artifact_integrity_problems(artifact)
    if problems:
        name = artifact.get("capability", {}).get("name", "artifact")
        raise ContractError(f"Artifact '{name}'", problems)
    return artifact


def validate_result(result):
    """Raise ContractError if the result envelope is malformed. Returns it otherwise."""
    problems = _schema_problems(result, RESULT_SCHEMA_PATH)
    if problems:
        raise ContractError(f"Result '{result.get('run_id', '?')}'", problems)
    return result


def load_artifact(path):
    with open(path, encoding="utf-8") as handle:
        return validate_artifact(json.load(handle))


def save_artifact(artifact, path):
    validate_artifact(artifact)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(artifact, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    return path
