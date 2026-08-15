"""The executor: perception and action against a live surface.

Shared by discovery and replay, which is why it is its own package rather than
living inside either. Discovery uses it to observe and act on a model's decisions;
replay uses the same code to resolve recorded targets and verify checkpoints. If
the two used different machinery, a capability could pass discovery and fail
replay for reasons that had nothing to do with the app.
"""

from .browser import ActionTimeout, BrowserSurface, TargetNotFound
from .surface import Element, Observation, Surface

__all__ = ["ActionTimeout", "BrowserSurface", "Element", "Observation", "Surface",
           "TargetNotFound"]
