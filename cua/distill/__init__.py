"""Trace → artifact.

Turns a recorded discovery trace into a capability artifact: templates
goal-derived literals into {inputs.*}, keeps credential tokens as {secrets.*},
classifies per-step risk from policy, synthesises checkpoints from observed
post-action state, and attaches the app's declared business outcomes. What the
tools recorded bounds what this can express.
"""

from pathlib import Path

import yaml

from .distiller import DistillationError, distil, slugify

ROOT = Path(__file__).resolve().parents[2]
OUTCOMES_PATH = ROOT / "outcomes.yaml"

__all__ = ["distil", "DistillationError", "slugify", "outcomes_for"]


def outcomes_for(app, path=None):
    """Business outcomes declared for an application (§5, §6).

    Missing file or unknown app yields an empty list — and that is a real claim,
    not a shrug: the artifact then declares it has no non-failure outcomes, which
    the schema records explicitly so a reviewer can see the omission rather than
    have it hidden.
    """
    path = Path(path or OUTCOMES_PATH)
    if not path.exists():
        return []
    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return list(config.get(app, []))
