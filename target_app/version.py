"""App identity and build stamp — the source of the artifact's `recorded_against`.

DESIGN.md §5 stamps every artifact with the app it was recorded against, so replay
can warn when a recording has drifted from the surface it was compiled for. That
field is only worth having if it can actually *change*: a fingerprint hardcoded on
the system side can never mismatch, and a check that can never fail is decoration.

So the target app reports its own identity, the way a real vendor app does — a
build number in the page chrome, plus a machine-readable generator tag. The
executor scrapes it at discovery time and re-scrapes it at replay.

`APP_BUILD` is overridable from the environment specifically so the drift path is
demonstrable without editing code:

    set TARGET_APP_BUILD=4.3.0      (Windows)
    TARGET_APP_BUILD=4.3.0          (POSIX)

Replay against a recording made at 4.2.1 then emits a drift warning.

Note this is a *declared* version: it catches changes someone remembered to
announce. Undeclared edits — a reordered form row, a renamed cell — need the
structural hash the executor computes from the observed DOM (Day 2). The two are
complementary; this one carries the human-readable story.
"""

import os

APP_NAME = "legacy-cu-portal"

# 4.3.0 — the member profile grew a sub-accounts table and its savings figure
#         became computed rather than static.
# 4.2.1 — initial build.
#
# Bumped by hand when the surface changes, which is the honest version of what a
# vendor does. Leaving it at 4.2.1 after changing the profile page would have made
# the fingerprint tell a small lie: artifacts recorded before and after would claim
# to have been recorded against the same app.
APP_BUILD = os.environ.get("TARGET_APP_BUILD", "4.3.0")
APP_FINGERPRINT = "{}@{}".format(APP_NAME, APP_BUILD)
