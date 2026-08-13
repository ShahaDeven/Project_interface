"""Fake credit-union operator portal — the target app (DESIGN.md §2).

A prop. It gets zero evaluation weight itself; it exists so the automation has a
realistic, controllable, deliberately hostile surface to work against.
"""

from .app import create_app, serve

__all__ = ["create_app", "serve"]
