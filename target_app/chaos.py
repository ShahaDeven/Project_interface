"""Chaos flags — runtime conditions injected on demand (DESIGN.md §2).

Arm/fire model: a request carrying `?chaos=<mode>` *arms* that mode for the
browser session and otherwise renders normally. The **next** page request in that
session fires it, one shot, and clears it. That matches the design wording ("on
next page load", "next action") and keeps the arming request itself clean, so a
replay can arm chaos on its opening navigate without corrupting that step.

Chaos is session-scoped (stored in the signed cookie) rather than process-global
because the replay browser must be the session that experiences it — a separate
curl/CLI request would arm a session nobody is driving.
"""

SLOW_SECONDS = 8

# mode -> the condition the replay engine is expected to classify it as
CHAOS_MODES = {
    "slow": "SLOW_LOAD",           # recoverable: wait/retry
    "session": "SESSION_EXPIRED",  # recoverable only if re-login is scripted
    "dialog": "UNKNOWN_DIALOG",    # -> NEEDS_INTERVENTION
    "error": "APP_ERROR",          # -> HARD_FAILURE
}

SESSION_KEY = "chaos_armed"

# Only top-level page loads arm or fire chaos. Sub-resources (the loan iframe,
# favicon, static files) must never swallow an armed flag — otherwise the browser
# consumes the chaos before the operator ever sees it.
CHAOS_ELIGIBLE_ENDPOINTS = {
    "login",
    "search",
    "member_profile",
    "subaccount_new",
    "subaccount_confirm",
}

DIALOG_TEXT = "Scheduled maintenance at 6 PM"
