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


@pytest.fixture(autouse=True)
def reset_target_app_state():
    """Sub-accounts opened by one test must not be visible to the next.

    The store is module-level and the live server shares this process, so without
    this a test that opens an account changes another test's expected balance —
    and the failure surfaces as an unrelated assertion, in a different file,
    depending on run order.
    """
    from target_app import data
    data.reset_sub_accounts()
    yield
    data.reset_sub_accounts()


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


# Ports Chromium refuses to connect to, no matter what is listening on them: it
# answers net::ERR_UNSAFE_PORT before a request leaves the browser. Mostly ports
# with a hijackable protocol of their own (SMTP, NNTP, H.323, IRC...).
#
# This matters because `live_server` binds to port 0 and the OS occasionally hands
# back one of them — 1720 once, which failed every browser test in the suite at
# once, with a message that looks nothing like "you drew a bad port". Rare enough
# to look like flakiness and total enough to look like a real regression, which is
# the worst combination to debug.
UNSAFE_PORTS = frozenset({
    1, 7, 9, 11, 13, 15, 17, 19, 20, 21, 22, 23, 25, 37, 42, 43, 53, 69, 77, 79,
    87, 95, 101, 102, 103, 104, 109, 110, 111, 113, 115, 117, 119, 123, 135, 137,
    139, 143, 161, 179, 389, 427, 465, 512, 513, 514, 515, 526, 530, 531, 532,
    540, 548, 554, 556, 563, 587, 601, 636, 989, 990, 993, 995, 1719, 1720, 1723,
    2049, 3659, 4045, 4190, 5060, 5061, 6000, 6566, 6665, 6666, 6667, 6668, 6669,
    6679, 6697, 10080,
})


@pytest.fixture(scope="session")
def live_server(app):
    """The target app on a real socket, for tests that drive a real browser.

    Bound to port 0 so the OS picks a free one — on Windows a stale server can
    keep answering on a port a new one appears to have bound (SO_REUSEADDR permits
    the duplicate bind), and a test suite silently talking to yesterday's build is
    a bad afternoon. The draw is repeated until it lands on a port Chromium is
    willing to talk to.
    """
    import socket
    import threading
    from werkzeug.serving import make_server

    for _ in range(20):
        server = make_server("127.0.0.1", 0, app, threaded=True)
        if server.socket.getsockname()[1] not in UNSAFE_PORTS:
            break
        server.server_close()
    else:
        raise RuntimeError("could not bind a port Chromium will connect to")

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
