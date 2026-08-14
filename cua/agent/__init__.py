"""Discovery loop: observe → decide → act, with a model in the decision loop.

Owns the six-tool action space (click, type, navigate, read, done, stuck), the
prompts, and the stopping conditions the loop enforces rather than the model
chooses (max 25 steps, 5-minute wall clock). Every request and response is logged
to the run's evidence directory.
"""

from .loop import DiscoveryLoop, DiscoveryResult
from .tools import ALL_TOOLS

__all__ = ["DiscoveryLoop", "DiscoveryResult", "ALL_TOOLS"]
