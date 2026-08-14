"""Allowlist enforcement (DESIGN §8).

Checked in the executor before every action, in discovery and replay alike. Two
properties matter more than the mechanism:

* **It is checked on the way in, not audited on the way out.** A blocked action
  never reaches the browser, so there is no window in which the agent has already
  done the thing we were going to disallow.
* **A violation ends the run.** Not a warning, not a skipped step. An agent that
  tried to leave the permitted surface has demonstrated it is operating on a model
  of the world we do not share, and the remaining steps are not trustworthy.

Origins are compared exactly (scheme, host, port). Routes match the path only:
query strings carry no authority, so `?chaos=dialog` cannot smuggle a route past
the list, and equally cannot lock a permitted one out.
"""

import re
from pathlib import Path
from urllib.parse import urlparse

import yaml

DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[2] / "policy.yaml"


class PolicyViolation(Exception):
    """An action outside the allowlist. Terminal: the run does not continue."""


class Allowlist:

    def __init__(self, allowed_origins, allowed_routes, allowed_actions):
        self.allowed_origins = [origin.rstrip("/") for origin in allowed_origins]
        self.allowed_routes = [re.compile(pattern) for pattern in allowed_routes]
        self.allowed_actions = set(allowed_actions)

    @classmethod
    def from_file(cls, path=None):
        path = Path(path or DEFAULT_POLICY_PATH)
        with open(path, encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
        missing = [key for key in ("allowed_origins", "allowed_routes", "allowed_actions")
                   if key not in config]
        if missing:
            # An allowlist with a silently-absent section is an allowlist that
            # permits everything in that dimension. Fail loudly instead.
            raise PolicyViolation(f"{path} is missing required section(s): {missing}")
        return cls(config["allowed_origins"], config["allowed_routes"], config["allowed_actions"])

    def origin_of(self, url):
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            raise PolicyViolation(f"navigate to '{url}' rejected: not an absolute URL")
        return f"{parsed.scheme}://{parsed.netloc}"

    def check_origin(self, url):
        """Raise PolicyViolation unless this origin is permitted at all.

        Separable from `check_navigate` so a run can be refused *before* a browser
        is launched or a model client is constructed. Discovering that a target is
        off-limits after paying for a model call is the wrong order.
        """
        origin = self.origin_of(url)
        if origin not in self.allowed_origins:
            raise PolicyViolation(
                f"'{url}' rejected: origin {origin} is not in the allowlist "
                f"({', '.join(self.allowed_origins)})")
        return origin

    def check_navigate(self, url):
        """Raise PolicyViolation unless this exact URL may be opened."""
        self.check_origin(url)

        path = urlparse(url).path or "/"
        if not any(pattern.match(path) for pattern in self.allowed_routes):
            raise PolicyViolation(
                f"navigate to '{url}' rejected: path '{path}' matches no allowed route")
        return url

    def check_action(self, action):
        if action not in self.allowed_actions:
            raise PolicyViolation(
                f"action '{action}' rejected: not in the allowed action list "
                f"({', '.join(sorted(self.allowed_actions))})")
        return action

    def permits_navigate(self, url):
        """Non-raising form, for reporting rather than enforcement."""
        try:
            self.check_navigate(url)
            return True
        except PolicyViolation:
            return False
