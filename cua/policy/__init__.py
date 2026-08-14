"""Guardrails, enforced in the executor for discovery and replay alike.

Allowlist of permitted origins, routes and action types; the risk gate that decides
what read_only, mutating and irreversible steps are allowed to do; and the
redaction layer that sits between raw observation and anything persisted, so
credentials and session tokens never reach a trace, artifact or log.

Risk gates and redaction arrive on Day 3.
"""

from .allowlist import Allowlist, PolicyViolation
from .redact import mask, shape_of
from .risk import RiskPolicy

__all__ = ["Allowlist", "PolicyViolation", "RiskPolicy", "mask", "shape_of"]
