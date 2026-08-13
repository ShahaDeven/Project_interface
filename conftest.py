"""Shared pytest fixtures.

Lives at the repo root so `import target_app` resolves no matter which directory
pytest is invoked from — a fresh clone should be able to run `pytest` and have it
work without setting PYTHONPATH.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from target_app import create_app  # noqa: E402


@pytest.fixture(scope="session")
def app():
    """The portal holds no mutable server state, so one app for the session is safe."""
    return create_app()


@pytest.fixture
def client(app):
    """Anonymous client — no session cookie."""
    return app.test_client()


@pytest.fixture
def operator(app):
    """Signed-in client. Credentials are irrelevant; the operator is hardcoded."""
    c = app.test_client()
    c.post("/login", data={"usr": "e.okafor", "pwd": "anything"})
    return c
