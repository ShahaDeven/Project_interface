"""Shared pytest fixtures.

Lives at the repo root so `import target_app` resolves no matter which directory
pytest is invoked from — a fresh clone should be able to run `pytest` and have it
work without setting PYTHONPATH.
"""

import sys
import time
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


@pytest.fixture(scope="session")
def live_server(app):
    """The target app on a real socket, for tests that drive a real browser.

    Bound to port 0 so the OS picks a free one — on Windows a stale server can
    keep answering on a port a new one appears to have bound (SO_REUSEADDR permits
    the duplicate bind), and a test suite silently talking to yesterday's build is
    a bad afternoon.
    """
    import socket
    import threading
    from werkzeug.serving import make_server

    server = make_server("127.0.0.1", 0, app, threaded=True)
    port = server.socket.getsockname()[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                break
    else:
        raise RuntimeError("live server did not come up")

    yield f"http://127.0.0.1:{port}"

    server.shutdown()
    thread.join(timeout=5)


@pytest.fixture(scope="session")
def playwright_instance():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as instance:
        yield instance


@pytest.fixture
def permissive_allowlist(live_server):
    """The real allowlist, retargeted at the ephemeral test port."""
    from cua.policy import Allowlist
    import yaml
    with open(ROOT / "policy.yaml", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    return Allowlist([live_server], config["allowed_routes"], config["allowed_actions"])


@pytest.fixture
def surface(playwright_instance, permissive_allowlist):
    from cua.executor import BrowserSurface
    browser = BrowserSurface(playwright_instance, allowlist=permissive_allowlist, headless=True)
    yield browser
    browser.close()
