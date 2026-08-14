"""Runtime configuration and credential resolution.

Secrets never reach the model. The agent is told to type the literal token
`{secrets.operator_password}`; the executor substitutes the real value at the
moment of the keystroke, and the trace records the token. So the credential is
absent from the transcript, absent from the step trace, absent from the distilled
artifact, and absent from the model's context — not filtered out of them
afterwards. §8's redaction rule holds because there is no point at which the
secret exists anywhere it could leak from.

Values come from the environment, or from a .env file that is gitignored.
"""

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"

DEFAULT_MODEL = "claude-sonnet-5"
MAX_TOKENS = 2048

# Stopping conditions are enforced by the loop, never chosen by the model (§4).
MAX_STEPS = 25
WALL_CLOCK_SECONDS = 300

SECRET_TOKEN_RE = re.compile(r"\{secrets\.([a-z][a-z0-9_]*)\}")
SECRET_ENV_PREFIX = "CUA_SECRET_"

_loaded = False


class MissingCredential(RuntimeError):
    """A required secret is not configured. Named, never valued, in the message."""


def load_env(path=ENV_PATH, force=False):
    """Populate os.environ from .env.

    A real environment variable always wins — a shell export is a deliberate act
    and should not be silently overridden by a file. Written by hand rather than
    taken as a dependency: it is fifteen lines and the alternative is another
    package for a reviewer to install.
    """
    global _loaded
    if _loaded and not force:
        return
    _loaded = True
    path = Path(path)
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(name, value)


def api_key():
    load_env()
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise MissingCredential(
            "ANTHROPIC_API_KEY is not set. Put it in .env (gitignored) or export it.")
    return key


def model():
    load_env()
    return os.environ.get("CUA_MODEL", DEFAULT_MODEL)


def secret(name):
    """Resolve one declared secret by name. Never logged, never returned in error text."""
    load_env()
    variable = SECRET_ENV_PREFIX + name.upper()
    value = os.environ.get(variable)
    if value is None:
        raise MissingCredential(
            f"secret '{name}' is not configured; set {variable} in .env or the environment")
    return value


def resolve_secrets(text):
    """Substitute {secrets.*} tokens. Called at the keystroke, nowhere earlier."""
    return SECRET_TOKEN_RE.sub(lambda match: secret(match.group(1)), text or "")


def secret_names_in(text):
    return SECRET_TOKEN_RE.findall(text or "")
