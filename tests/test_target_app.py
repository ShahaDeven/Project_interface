"""Tests for the target app (DESIGN.md §2).

The app is a prop and gets no evaluation weight of its own — but the artifacts the
system records depend on it behaving *exactly* as specified, so these tests are
really a contract check on the surface everything downstream is built against.

Two conventions worth knowing before editing this file:

1.  **Checkpoint strings are hardcoded, deliberately.** "Member Profile",
    "No member matches this number", "Member outside your region" and friends are
    literals in saved artifacts and in the replay engine's outcome scanner. If a
    template rename could quietly update both the app and its tests, the tests
    would stop protecting anything. So they are typed out here by hand.
2.  Structural rules (no `id`, no `data-*`, no semantic HTML5) are asserted by one
    shared helper applied to every operator-facing page, so adding a page means
    adding one line to `PAGES` rather than remembering the whole rule set.
"""

import re
import time

import pytest

from target_app import create_app
from target_app import data as d
from target_app import version as v
from target_app.chaos import SLOW_SECONDS

# --- literals that downstream contracts depend on -----------------------------
LOGIN_CHECKPOINT = "Operator Login"
PROFILE_CHECKPOINT = "Member Profile"
SUBACCOUNT_CHECKPOINT = "Sub-account opened"
NOT_FOUND_MARKER = "No member matches this number"
DENIED_MARKER = "Member outside your region"
APP_ERROR_TITLE = "HTTP 500 — Internal Server Error"  # em dash
DIALOG_MARKER = "Scheduled maintenance at 6 PM"

# --- demo fixtures from DESIGN.md §10 -----------------------------------------
DISCOVERY_ID = "12345"   # recorded against
REPLAY_ID = "23456"      # replayed with, deliberately different from recorded
DENIED_ID = "67890"      # Western -> PERMISSION_DENIED
MISSING_ID = "99999"     # -> MEMBER_NOT_FOUND

VALID_SUBACCOUNT = {
    "ddlActType": "Holiday Club",
    "txtNick": "Vacation fund",
    "txtDep": "150.00",
    "ddlFund": "Cash at branch",
    "rdoStmt": "Mail",
    "chkOd": "Y",
}


def body_of(response):
    return response.data.decode("utf-8", "replace")


# =============================================================================
# Auth gate
# =============================================================================

class TestAuthGate:

    @pytest.mark.parametrize("path", [
        "/",
        "/search",
        f"/member/{DISCOVERY_ID}",
        f"/member/{DISCOVERY_ID}/sub-account/new",
    ])
    def test_anonymous_access_is_bounced_to_login(self, client, path):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 302
        assert "/login" in response.headers["Location"]

    def test_login_page_carries_its_checkpoint_text(self, client):
        assert LOGIN_CHECKPOINT in body_of(client.get("/login"))

    def test_any_credentials_are_accepted(self, client):
        response = client.post("/login", data={"usr": "zzz", "pwd": "zzz"})
        assert response.status_code == 302
        assert "/search" in response.headers["Location"]

    def test_operator_identity_is_hardcoded(self, operator):
        page = body_of(operator.get("/search"))
        assert d.OPERATOR_NAME in page
        assert d.OPERATOR_REGION in page


# =============================================================================
# Member lookup — the deterministic rules the evidence set depends on
# =============================================================================

class TestMemberLookup:

    def test_valid_eastern_number_redirects_to_the_profile(self, operator):
        response = operator.get(f"/search?txtMbr={DISCOVERY_ID}")
        assert response.status_code == 302
        assert f"/member/{DISCOVERY_ID}" in response.headers["Location"]

    def test_profile_renders_checkpoint_and_extractable_fields(self, operator):
        page = body_of(operator.get(f"/member/{DISCOVERY_ID}"))
        assert PROFILE_CHECKPOINT in page
        assert "Alice Torres" in page          # lookup_member_balance -> member_name
        assert "$4,523.18" in page             # lookup_member_balance -> savings_balance
        assert "Member name" in page           # label-strategy anchor
        assert "Savings balance" in page

    def test_profile_embeds_a_frame(self, operator):
        assert "<iframe" in body_of(operator.get(f"/member/{DISCOVERY_ID}"))

    def test_loan_details_are_only_reachable_inside_the_frame(self, operator):
        profile = body_of(operator.get(f"/member/{DISCOVERY_ID}"))
        frame = body_of(operator.get(f"/member/{DISCOVERY_ID}/loan-frame"))
        assert "Active loan on file" in frame
        assert "$15,000.00" in frame
        assert "Active loan on file" not in profile

    @pytest.mark.parametrize("member_id", sorted(
        mid for mid, m in d.MEMBERS.items() if m["region"] == "Western"))
    def test_western_members_are_denied(self, operator, member_id):
        response = operator.get(f"/member/{member_id}")
        assert response.status_code == 403
        assert DENIED_MARKER in body_of(response)

    def test_denial_also_covers_the_mutation_route(self, operator):
        """Region scope is enforced per-request, not only on the profile page."""
        response = operator.get(f"/member/{DENIED_ID}/sub-account/new")
        assert response.status_code == 403
        assert DENIED_MARKER in body_of(response)

    def test_unknown_number_reports_no_match_from_search(self, operator):
        assert NOT_FOUND_MARKER in body_of(operator.get(f"/search?txtMbr={MISSING_ID}"))

    def test_unknown_number_reports_no_match_on_direct_navigation(self, operator):
        response = operator.get(f"/member/{MISSING_ID}")
        assert response.status_code == 404
        assert NOT_FOUND_MARKER in body_of(response)

    @pytest.mark.parametrize("raw", ["abc", "123", "123456", "12 45", "1234x"])
    def test_malformed_member_numbers_are_rejected(self, operator, raw):
        assert "exactly 5 digits" in body_of(operator.get("/search", query_string={"txtMbr": raw}))

    def test_empty_search_renders_the_bare_form(self, operator):
        page = body_of(operator.get("/search"))
        assert "Member number" in page
        assert NOT_FOUND_MARKER not in page

    def test_seed_data_agrees_with_the_id_range_rule(self):
        """The range rule is the contract; the seed must never contradict it."""
        for member_id, member in d.MEMBERS.items():
            assert d.region_for_id(member_id) == member["region"], member_id

    def test_every_demo_id_behaves_as_the_evidence_set_expects(self):
        assert d.get_member(DISCOVERY_ID)["region"] == "Eastern"
        assert d.get_member(REPLAY_ID)["region"] == "Eastern"
        assert d.get_member(DENIED_ID)["region"] == "Western"
        assert d.get_member(MISSING_ID) is None


# =============================================================================
# Mutation flow — the surface the risk gate and HITL confirmation hang off
# =============================================================================

class TestMutationFlow:

    def test_form_renders(self, operator):
        page = body_of(operator.get(f"/member/{REPLAY_ID}/sub-account/new"))
        assert "Open Sub-Account" in page
        for label in ["Account type", "Account nickname", "Initial deposit",
                      "Funding source", "Statement delivery", "Overdraft link"]:
            assert label in page

    def test_form_leads_to_a_confirmation_screen(self, operator):
        page = body_of(operator.post(f"/member/{REPLAY_ID}/sub-account/new",
                                     data=VALID_SUBACCOUNT))
        assert "Confirm Sub-Account" in page
        assert "Confirm and Open Account" in page

    def test_confirmation_screen_does_not_open_the_account(self, operator):
        """The irreversible step is the confirm submit, and nothing before it."""
        page = body_of(operator.post(f"/member/{REPLAY_ID}/sub-account/new",
                                     data=VALID_SUBACCOUNT))
        assert SUBACCOUNT_CHECKPOINT not in page

    def test_confirm_opens_the_account(self, operator):
        page = body_of(operator.post(f"/member/{REPLAY_ID}/sub-account/confirm",
                                     data=VALID_SUBACCOUNT))
        assert SUBACCOUNT_CHECKPOINT in page
        assert re.search(rf"SA-{REPLAY_ID}-\d\d", page)

    def test_account_number_is_deterministic(self, operator):
        """Same inputs must produce byte-identical evidence on a re-run."""
        first = body_of(operator.post(f"/member/{REPLAY_ID}/sub-account/confirm",
                                      data=VALID_SUBACCOUNT))
        second = body_of(operator.post(f"/member/{REPLAY_ID}/sub-account/confirm",
                                       data=VALID_SUBACCOUNT))
        number = re.search(rf"SA-{REPLAY_ID}-\d\d", first).group(0)
        assert number in second

    @pytest.mark.parametrize("field,value,expected", [
        ("txtNick", "", "nickname is required"),
        ("txtDep", "5", "at least $25.00"),
        ("txtDep", "abc", "not a valid amount"),
        ("ddlActType", "Fake Account", "Select an account type"),
        ("ddlFund", "Suitcase", "Select a funding source"),
        ("rdoStmt", "", "Select a statement delivery method"),
    ])
    def test_invalid_submissions_return_to_the_form(self, operator, field, value, expected):
        payload = {**VALID_SUBACCOUNT, field: value}
        page = body_of(operator.post(f"/member/{REPLAY_ID}/sub-account/new", data=payload))
        assert expected in page
        assert "Confirm and Open Account" not in page

    def test_the_opened_account_appears_on_the_profile(self, operator):
        """The irreversible step has to leave a mark a later page can see, or the
        risk gate is guarding an illusion."""
        assert "No sub-accounts on file" in body_of(operator.get(f"/member/{REPLAY_ID}"))

        operator.post(f"/member/{REPLAY_ID}/sub-account/confirm", data=VALID_SUBACCOUNT)

        page = body_of(operator.get(f"/member/{REPLAY_ID}"))
        assert re.search(rf"SA-{REPLAY_ID}-\d\d", page)
        assert "Vacation fund" in page
        assert "$150.00" in page

    def test_savings_falls_only_when_funded_from_savings(self, operator):
        """Cash at the branch funds the new account from elsewhere, so the member's
        savings are untouched. A transfer out of savings is what moves the figure."""
        before = body_of(operator.get(f"/member/{REPLAY_ID}"))
        assert "$18,240.55" in before

        operator.post(f"/member/{REPLAY_ID}/sub-account/confirm", data=VALID_SUBACCOUNT)
        assert "$18,240.55" in body_of(operator.get(f"/member/{REPLAY_ID}"))

        operator.post(f"/member/{REPLAY_ID}/sub-account/confirm",
                      data={**VALID_SUBACCOUNT, "ddlFund": "Transfer from primary savings"})
        assert "$18,090.55" in body_of(operator.get(f"/member/{REPLAY_ID}"))

    def test_opened_accounts_do_not_survive_a_restart(self):
        """State lives in process memory on purpose: a demo starts from a known
        state by starting the server, so seeded data stays deterministic."""
        assert d.sub_accounts_for(REPLAY_ID) == []

    def test_confirm_route_revalidates(self, operator):
        """A tampered confirm POST must not slip past the form's rules."""
        payload = {**VALID_SUBACCOUNT, "txtDep": "1"}
        page = body_of(operator.post(f"/member/{REPLAY_ID}/sub-account/confirm", data=payload))
        assert SUBACCOUNT_CHECKPOINT not in page
        assert "at least $25.00" in page


# =============================================================================
# Chaos flags — injected runtime conditions (DESIGN.md §2, consumed by §6)
# =============================================================================

class TestChaosFlags:

    @pytest.mark.parametrize("mode", ["slow", "session", "dialog", "error"])
    def test_the_arming_request_itself_renders_normally(self, operator, mode):
        """Arming must not corrupt the step that arms it."""
        response = operator.get(f"/search?chaos={mode}")
        assert response.status_code == 200
        assert DIALOG_MARKER not in body_of(response)

    def test_dialog_appears_on_the_next_page_then_clears(self, operator):
        operator.get("/search?chaos=dialog")
        assert DIALOG_MARKER in body_of(operator.get(f"/member/{DISCOVERY_ID}"))
        assert DIALOG_MARKER not in body_of(operator.get(f"/member/{DISCOVERY_ID}"))

    def test_error_returns_a_500_page_then_clears(self, operator):
        operator.get("/search?chaos=error")
        response = operator.get(f"/member/{DISCOVERY_ID}")
        assert response.status_code == 500
        assert APP_ERROR_TITLE in body_of(response)
        assert operator.get(f"/member/{DISCOVERY_ID}").status_code == 200

    def test_session_expiry_bounces_to_login_and_is_recoverable(self, operator):
        operator.get("/search?chaos=session")
        response = operator.get(f"/member/{DISCOVERY_ID}")
        assert response.status_code == 302
        assert "/login" in response.headers["Location"]

        operator.post("/login", data={"usr": "e.okafor", "pwd": "x"})
        assert operator.get(f"/member/{DISCOVERY_ID}").status_code == 200

    @pytest.mark.slow
    def test_slow_delays_one_page_load_then_clears(self, operator):
        operator.get("/search?chaos=slow")

        started = time.monotonic()
        operator.get(f"/member/{DISCOVERY_ID}")
        delayed = time.monotonic() - started

        started = time.monotonic()
        operator.get(f"/member/{DISCOVERY_ID}")
        normal = time.monotonic() - started

        assert delayed >= SLOW_SECONDS - 0.2
        assert normal < 1.0

    def test_sub_resources_do_not_swallow_an_armed_flag(self, operator):
        """Chromium fetches /favicon.ico and the loan iframe on its own. If either
        consumed the armed flag, the operator would never see the condition."""
        operator.get("/search?chaos=dialog")
        operator.get("/favicon.ico")
        operator.get(f"/member/{DISCOVERY_ID}/loan-frame")
        assert DIALOG_MARKER in body_of(operator.get(f"/member/{DISCOVERY_ID}"))

    def test_unknown_chaos_value_is_ignored(self, operator):
        response = operator.get("/search?chaos=banana")
        assert response.status_code == 200
        assert operator.get(f"/member/{DISCOVERY_ID}").status_code == 200


# =============================================================================
# Self-reported build — the source of recorded_against.app_fingerprint (§5)
# =============================================================================

class TestAppFingerprint:

    def test_fingerprint_is_app_at_build(self):
        assert v.APP_NAME == "legacy-cu-portal"
        assert v.APP_FINGERPRINT == f"{v.APP_NAME}@{v.APP_BUILD}"

    @pytest.mark.parametrize("path", [
        "/login",
        "/search",
        f"/member/{DISCOVERY_ID}",
        f"/member/{DENIED_ID}",
        f"/member/{MISSING_ID}",
        f"/member/{REPLAY_ID}/sub-account/new",
    ])
    def test_every_operator_page_reports_the_fingerprint(self, operator, path):
        """Scrapeable wherever discovery or replay happens to start."""
        assert f'<meta name="generator" content="{v.APP_FINGERPRINT}">' in body_of(
            operator.get(path))

    def test_masthead_shows_the_build_to_a_human(self, operator):
        assert f"build {v.APP_BUILD}" in body_of(operator.get("/search"))

    def test_build_is_overridable_from_the_environment(self, monkeypatch):
        """The drift demo bumps the build without touching code, so the executor
        can be shown warning on a real mismatch rather than a staged one.

        The override value is deliberately one the app will never ship. Using a
        plausible next version would make this test pass the day that version
        becomes the default, whether or not the override still worked."""
        assert v.APP_BUILD != "9.9.9", "pick an override the app will never ship"

        monkeypatch.setenv("TARGET_APP_BUILD", "9.9.9")
        import importlib
        reloaded = importlib.reload(v)
        try:
            assert reloaded.APP_FINGERPRINT == "legacy-cu-portal@9.9.9"
            page = body_of(create_app().test_client().get("/login"))
            assert 'content="legacy-cu-portal@9.9.9"' in page
            assert "build 9.9.9" in page
        finally:
            monkeypatch.delenv("TARGET_APP_BUILD")
            importlib.reload(v)

    def test_crashed_app_reports_nothing(self, operator):
        """A 500 page carries no fingerprint. Realistic, and it keeps the executor
        honest: absent must be handled as 'unknown', never as 'unchanged'."""
        operator.get("/search?chaos=error")
        assert "generator" not in body_of(operator.get(f"/member/{DISCOVERY_ID}"))

    def test_fingerprint_is_not_an_element_hook(self, operator):
        """It names the application, not any element — the locator problem is
        untouched by its presence."""
        page = body_of(operator.get(f"/member/{DISCOVERY_ID}"))
        head = page[:page.lower().index("</head>")]
        assert v.APP_FINGERPRINT in head
        assert v.APP_FINGERPRINT not in page[page.lower().index("</head>"):]


# =============================================================================
# Hostile markup — the reason we build the target app instead of using a real one
# =============================================================================

SEMANTIC_TAGS = ["<header", "<nav", "<main", "<footer", "<section", "<article",
                 "<aside", "<figure", "<label"]

PAGES = [
    ("login",           "get",  "/login",                                   None),
    ("search",          "get",  "/search",                                  None),
    ("search-no-match", "get",  f"/search?txtMbr={MISSING_ID}",             None),
    ("profile",         "get",  f"/member/{DISCOVERY_ID}",                  None),
    ("loan-frame",      "get",  f"/member/{DISCOVERY_ID}/loan-frame",       None),
    ("denied",          "get",  f"/member/{DENIED_ID}",                     None),
    ("not-found",       "get",  f"/member/{MISSING_ID}",                    None),
    ("subaccount-form", "get",  f"/member/{REPLAY_ID}/sub-account/new",     None),
    ("subaccount-confirm", "post", f"/member/{REPLAY_ID}/sub-account/new",  VALID_SUBACCOUNT),
    ("subaccount-done", "post", f"/member/{REPLAY_ID}/sub-account/confirm", VALID_SUBACCOUNT),
]


def assert_hostile_markup(page, label):
    """The §2 rules, in one place, so every page is held to the same standard."""
    lowered = page.lower()
    assert not re.search(r"\sid\s*=", lowered), f"{label}: has an id attribute"
    assert "data-" not in lowered, f"{label}: has a data-* hook"
    offenders = [tag for tag in SEMANTIC_TAGS if tag in lowered]
    assert not offenders, f"{label}: semantic markup {offenders}"
    assert "<table" in lowered, f"{label}: not table-laid-out"


class TestHostileMarkup:

    @pytest.mark.parametrize("label,method,path,payload",
                             PAGES, ids=[p[0] for p in PAGES])
    def test_page_obeys_the_hostility_rules(self, operator, label, method, path, payload):
        response = getattr(operator, method)(path, data=payload)
        assert_hostile_markup(body_of(response), label)

    def test_error_page_obeys_them_too(self, operator):
        operator.get("/search?chaos=error")
        assert_hostile_markup(body_of(operator.get(f"/member/{DISCOVERY_ID}")), "error500")

    def test_layout_nests_tables(self, operator):
        assert body_of(operator.get(f"/member/{DISCOVERY_ID}")).lower().count("<table") >= 3

    def test_class_names_are_generic_and_reused(self, operator):
        page = body_of(operator.get(f"/member/{DISCOVERY_ID}"))
        for name in ["c1", "c2", "row1", "row2"]:
            assert page.count(f'class="{name}"') > 1, f"{name} is not reused"

    def test_form_fields_are_labelled_only_by_adjacent_cells(self, operator):
        """No <label>, no placeholder, no title — the visible <td> text is the
        only signal, which is what the label-strategy has to key off."""
        page = body_of(operator.get(f"/member/{REPLAY_ID}/sub-account/new"))
        assert "placeholder=" not in page.lower()
        assert "aria-" not in page.lower()
        assert "Account nickname" in page
