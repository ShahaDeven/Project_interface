"""Per-step risk classification (DESIGN §5, §8).

Every step carries `read_only`, `mutating` or `irreversible`, and the replay gate
hangs off it. The question is who decides.

Not the model: an agent asked "was that dangerous?" will answer plausibly rather
than correctly, and the answer would be baked into an artifact forever.

Not a heuristic on the button's wording either — "Submit" and "Confirm" are the
same word on a search form and on an account opening.

So it is configuration, matched against what the browser actually did: the HTTP
method of the form that was submitted, and the route it was submitted to. A GET is
read-only by construction. A POST is a mutation *unless* its route says otherwise
(signing in POSTs, and creates nothing). A POST to a route on the irreversible list
always pauses for a human, discovery and replay alike.

Putting the lists in policy.yaml rather than in code is the point: which routes are
irreversible is an operational decision about a particular deployment, it differs
per institution, and it must be reviewable by someone who does not read Python.
"""

import re
from urllib.parse import urlparse

READ_ONLY = "read_only"
MUTATING = "mutating"
IRREVERSIBLE = "irreversible"


class RiskPolicy:

    def __init__(self, mutating_routes=(), irreversible_routes=(), read_only_routes=()):
        self.mutating = [re.compile(p) for p in mutating_routes]
        self.irreversible = [re.compile(p) for p in irreversible_routes]
        self.read_only = [re.compile(p) for p in read_only_routes]

    @classmethod
    def from_config(cls, config):
        section = (config or {}).get("risk", {}) or {}
        return cls(
            mutating_routes=section.get("mutating_routes", []),
            irreversible_routes=section.get("irreversible_routes", []),
            read_only_routes=section.get("read_only_routes", []),
        )

    @staticmethod
    def _path(action):
        """Form actions are often relative; only the path is ever matched."""
        if not action:
            return ""
        parsed = urlparse(action)
        return parsed.path or action

    def classify(self, action, form=None):
        """Risk of one recorded step.

        `form` is the owning form descriptor of a clicked control, when there was
        one. Anything that does not submit a form cannot change server state.
        """
        if action in ("navigate", "read", "type"):
            return READ_ONLY
        if action != "click" or not form:
            return READ_ONLY

        method = (form.get("method") or "get").lower()
        path = self._path(form.get("action"))

        if any(pattern.match(path) for pattern in self.irreversible):
            return IRREVERSIBLE
        if method != "post":
            return READ_ONLY
        if any(pattern.match(path) for pattern in self.read_only):
            return READ_ONLY
        if any(pattern.match(path) for pattern in self.mutating):
            return MUTATING
        # An unrecognised POST is treated as a mutation. The conservative default
        # is the right one here: the cost of over-classifying is an approval flag
        # on a replay, and the cost of under-classifying is an unreviewed write to
        # a member's account.
        return MUTATING
