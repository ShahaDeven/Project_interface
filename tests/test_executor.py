"""Tests for the executor: perception, action and the allowlist (DESIGN §3, §8).

These drive a real Chromium against the real target app on a real socket. Nothing
here is mocked, because the thing under test *is* the interaction with a hostile
DOM — a mocked page would only prove the mock was hostile in the ways we thought
of.

Marked `browser`; run `pytest -m "not browser"` to skip them.
"""

import pytest

from cua.executor import TargetNotFound
from cua.policy import Allowlist, PolicyViolation

pytestmark = pytest.mark.browser


def sign_in(surface, base_url):
    surface.navigate(f"{base_url}/login")
    observation = surface.observe()
    for element in observation.interactive():
        if element.role in ("textbox", "password"):
            surface.type(element, "e.okafor")
    button = next(e for e in observation.interactive() if e.label == "Sign On")
    surface.click(button)
    return surface.observe()


def labelled(observation, label):
    return next(e for e in observation.elements if e.label == label)


# =============================================================================
# Distillation on a page with no ids, no hooks and no semantic markup
# =============================================================================

class TestDistillation:

    def test_login_controls_are_named_by_their_adjacent_cells(self, surface, live_server):
        """The only thing naming these fields is the text in the cell beside them."""
        surface.navigate(f"{live_server}/login")
        labels = {e.label for e in surface.observe().interactive()}
        assert {"Operator ID", "Password", "Branch", "Sign On"} <= labels

    def test_password_value_is_never_observed(self, surface, live_server):
        """Redaction starts at perception: an unobserved value cannot leak into a
        trace, a screenshot description, or a distilled artifact."""
        surface.navigate(f"{live_server}/login")
        observation = surface.observe()
        password = next(e for e in observation.interactive() if e.role == "password")
        surface.type(password, "hunter2")
        after = surface.observe()
        assert next(e for e in after.interactive() if e.role == "password").text == ""
        assert "hunter2" not in after.render()

    def test_data_cells_are_addressable_for_reading(self, surface, live_server):
        """A savings balance is not interactive. If only controls were collected,
        `read` would be unusable and extraction would fall back to OCR."""
        sign_in(surface, live_server)
        surface.navigate(f"{live_server}/member/12345")
        observation = surface.observe()
        assert surface.read(labelled(observation, "Member name")) == "Alice Torres"
        assert surface.read(labelled(observation, "Savings balance")) == "$4,523.18"

    def test_frame_contents_are_visible(self, surface, live_server):
        """Loan details live in an unnamed iframe. Stopping at the main document
        would simply miss them, which is what real legacy apps count on."""
        sign_in(surface, live_server)
        surface.navigate(f"{live_server}/member/12345")
        observation = surface.observe()
        framed = [e for e in observation.elements if e.frame]
        assert framed, "no elements found inside the iframe"
        assert surface.read(labelled(observation, "Loan amount")) == "$15,000.00"

    def test_elements_carry_all_three_strategies(self, surface, live_server):
        """Discovery is the only moment all three can be observed together."""
        surface.navigate(f"{live_server}/login")
        button = labelled(surface.observe(), "Sign On")
        kinds = [s["kind"] for s in button.as_strategies()]
        assert kinds == ["label", "structural", "coordinates"]
        assert button.structural.startswith("body")
        assert all(n > 0 for n in button.center)

    def test_coordinates_never_travel_without_verification(self, surface, live_server):
        surface.navigate(f"{live_server}/login")
        button = labelled(surface.observe(), "Sign On")
        coordinates = next(s for s in button.as_strategies() if s["kind"] == "coordinates")
        assert coordinates["verify_text_nearby"] == "Sign On"

    def test_observation_renders_for_the_model(self, surface, live_server):
        surface.navigate(f"{live_server}/login")
        rendered = surface.observe().render()
        assert "Operator Login" in rendered or "Operator ID" in rendered
        assert "[0]" in rendered

    def test_fingerprint_is_read_from_the_page(self, surface, live_server):
        """Compared against what the app reports, not against a literal — a
        hardcoded build here breaks on every legitimate version bump, which trains
        people to edit the test instead of asking why it changed."""
        from target_app import version

        surface.navigate(f"{live_server}/login")
        assert surface.observe().app_fingerprint == version.APP_FINGERPRINT
        assert surface.observe().app_fingerprint.startswith("legacy-cu-portal@")


# =============================================================================
# Acting, and the strategy fallback chain
# =============================================================================

class TestActions:

    def test_a_full_lookup_can_be_driven(self, surface, live_server):
        sign_in(surface, live_server)
        observation = surface.observe()
        surface.type(labelled(observation, "Member number"), "23456")
        surface.click(labelled(observation, "Look Up"))
        assert surface.text_present("Member Profile")
        assert surface.read(labelled(surface.observe(), "Member name")) == "Marcus Bell"

    def test_label_strategy_survives_a_structural_change(self, surface, live_server):
        """The point of the chain, as a falsifiable claim rather than a hopeful one:
        after a row is inserted above the field, the recorded structural path stops
        resolving *and* the adjacent-cell label still finds it. Asserting only that
        label won would prove nothing — label is tried first regardless."""
        sign_in(surface, live_server)
        field = labelled(surface.observe(), "Member number")
        structural = next(s for s in field.as_strategies() if s["kind"] == "structural")
        label = next(s for s in field.as_strategies() if s["kind"] == "label")

        # Reached via the input, not via the form: the HTML parser hoists a <form>
        # out of table structure, so `form tr` matches nothing on this page. That
        # quirk is period-accurate and worth knowing about — it is exactly the sort
        # of thing that makes structural paths on legacy markup untrustworthy.
        surface.page.evaluate("""() => {
            const row = document.querySelector('input[type=text]').closest('tr');
            const filler = document.createElement('tr');
            filler.innerHTML = '<td>filler</td><td>filler</td>';
            row.parentNode.insertBefore(filler, row);
        }""")

        with pytest.raises(TargetNotFound):
            surface._resolve(field, strategies=[structural])

        surface._resolve(field, strategies=[label])
        assert surface.last_strategy.startswith("label:")

    def test_typing_into_a_dropdown_selects_an_option(self, surface, live_server):
        """`type` is the only value-setting verb, so it has to mean the right thing
        per control. fill() throws outright on a <select>."""
        surface.navigate(f"{live_server}/login")
        branch = labelled(surface.observe(), "Branch")
        assert branch.role == "select"
        surface.type(branch, "W07 — Western Plaza")
        assert surface.read(labelled(surface.observe(), "Branch")) == "W07"

    def test_typing_into_a_checkbox_sets_its_state(self, surface, live_server):
        sign_in(surface, live_server)
        surface.navigate(f"{live_server}/member/12345/sub-account/new")
        box = labelled(surface.observe(), "Link to primary savings")
        surface.type(box, "yes")
        assert surface.page.locator("input[type=checkbox]").is_checked()
        surface.type(box, "no")
        assert not surface.page.locator("input[type=checkbox]").is_checked()

    def test_radios_in_one_group_are_told_apart(self, surface, live_server):
        """Every radio in a group shares a cell, so the cell label names all of
        them identically. Resolving that would pick whichever came first and
        report success — a wrong answer, not a failure."""
        sign_in(surface, live_server)
        surface.navigate(f"{live_server}/member/12345/sub-account/new")
        observation = surface.observe()
        radios = [e for e in observation.interactive() if e.role == "radio"]
        assert {e.label for e in radios} == {"Mail", "Electronic"}

        surface.click(labelled(observation, "Electronic"))
        assert surface.page.locator('input[value="Electronic"]').is_checked()
        assert not surface.page.locator('input[value="Mail"]').is_checked()
        assert surface.last_strategy.startswith("label:")

    def test_the_whole_sub_account_form_can_be_filled(self, surface, live_server):
        """The flow open_sub_account will be recorded against — a select, radios,
        a checkbox and two text fields, none of them with a label element."""
        sign_in(surface, live_server)
        surface.navigate(f"{live_server}/member/12345/sub-account/new")
        observation = surface.observe()
        surface.type(labelled(observation, "Account type"), "Holiday Club")
        surface.type(labelled(observation, "Account nickname"), "Vacation fund")
        surface.type(labelled(observation, "Initial deposit"), "150.00")
        surface.type(labelled(observation, "Funding source"), "Cash at branch")
        surface.click(labelled(observation, "Mail"))
        surface.click(labelled(observation, "Review"))
        assert surface.text_present("Confirm Sub-Account")

    def test_stale_coordinates_are_refused(self, surface, live_server):
        """Never click blind: if the recorded text is not near the point any more,
        the strategy fails rather than clicking whatever moved there."""
        surface.navigate(f"{live_server}/login")
        button = labelled(surface.observe(), "Sign On")
        with pytest.raises(TargetNotFound):
            surface._resolve(button, strategies=[{
                "kind": "coordinates",
                "value": [button.center[0], button.center[1]],
                "verify_text_nearby": "Wire Transfer",
            }])

    def test_unresolvable_target_names_what_was_tried(self, surface, live_server):
        """A failure has to be debuggable: which strategies, in what order."""
        surface.navigate(f"{live_server}/login")
        button = labelled(surface.observe(), "Sign On")
        surface.page.evaluate("() => document.body.innerHTML = '<p>gone</p>'")
        with pytest.raises(TargetNotFound) as raised:
            surface._resolve(button)
        message = str(raised.value)
        assert "label:" in message and "structural:" in message

    def test_text_present_searches_inside_frames(self, surface, live_server):
        sign_in(surface, live_server)
        surface.navigate(f"{live_server}/member/12345")
        assert surface.text_present("Active loan on file")

    def test_screenshot_is_written(self, surface, live_server, tmp_path):
        surface.navigate(f"{live_server}/login")
        path = tmp_path / "shots" / "login.png"
        surface.observe(screenshot_path=path)
        assert path.exists() and path.stat().st_size > 0


# =============================================================================
# Allowlist — checked on the way in, never audited on the way out
# =============================================================================

class TestAllowlist:

    def test_offsite_navigation_is_blocked_before_the_browser_sees_it(self, surface):
        with pytest.raises(PolicyViolation) as raised:
            surface.navigate("https://example.com/")
        assert "origin" in str(raised.value)
        assert surface.page.url == "about:blank", "the page moved despite the block"

    def test_unlisted_route_on_a_permitted_origin_is_blocked(self, surface, live_server):
        with pytest.raises(PolicyViolation) as raised:
            surface.navigate(f"{live_server}/admin/wire-transfer")
        assert "matches no allowed route" in str(raised.value)

    def test_query_strings_carry_no_authority(self, surface, live_server):
        """Chaos flags must not be able to smuggle a route past the list, nor lock
        a permitted one out. Signed in first: an anonymous request is bounced to
        /login by the app, which would hide whether the allowlist permitted it."""
        sign_in(surface, live_server)
        surface.navigate(f"{live_server}/search?chaos=dialog")
        assert "/search" in surface.page.url

    def test_relative_url_is_rejected(self, permissive_allowlist):
        with pytest.raises(PolicyViolation):
            permissive_allowlist.check_navigate("/member/12345")

    def test_action_types_are_allowlisted_too(self, permissive_allowlist):
        with pytest.raises(PolicyViolation):
            permissive_allowlist.check_action("download")

    def test_missing_policy_section_fails_loudly(self, tmp_path):
        """An absent section is not an empty rule — it is 'permit everything'."""
        path = tmp_path / "policy.yaml"
        path.write_text("allowed_origins: []\n", encoding="utf-8")
        with pytest.raises(PolicyViolation) as raised:
            Allowlist.from_file(path)
        assert "missing required section" in str(raised.value)

    def test_real_policy_file_loads(self):
        allowlist = Allowlist.from_file()
        assert allowlist.permits_navigate("http://127.0.0.1:5000/member/12345")
        assert not allowlist.permits_navigate("http://127.0.0.1:5000/etc/passwd")
        assert not allowlist.permits_navigate("http://evil.test/login")
