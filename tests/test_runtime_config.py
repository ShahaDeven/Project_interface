"""Tests for the runtime-condition recognisers (DESIGN §6).

Config that is only exercised when something has already gone wrong is config that
rots silently — a malformed re-login routine is discovered during a session
recovery, which is the worst possible moment. These load and check it up front.
"""

import textwrap

import pytest

from cua.replay import RuntimeConfig, RuntimeConfigError
from cua.replay.runtime import SlowLoadPolicy

APP = "legacy-cu-portal"


def write(tmp_path, body):
    path = tmp_path / "runtime.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


@pytest.fixture
def config():
    return RuntimeConfig.load(APP)


class TestShippedProfile:

    def test_the_target_app_has_a_profile(self, config):
        assert config.profiled
        assert config.app == APP

    def test_app_error_is_recognised_by_the_500_marker(self, config):
        assert config.app_error[0]["value"] == "HTTP 500 — Internal Server Error"

    def test_session_expiry_is_recoverable_because_a_routine_exists(self, config):
        """§6: recoverable *only* if a relogin routine is defined for the app."""
        assert config.session.recoverable
        assert len(config.session.relogin) == 3
        assert config.session.relogin[-1]["checkpoint"]["value"] == "Member Lookup"

    def test_the_relogin_routine_uses_secret_tokens_not_values(self, config):
        values = [step.get("value") for step in config.session.relogin]
        assert "{secrets.operator_id}" in values
        assert "{secrets.operator_password}" in values

    def test_dialogs_are_detected_structurally(self, config):
        """No portable way to ask a page whether a modal covers it, so how this
        app renders one is declared and the rule becomes: anything matching that
        which is not named is unknown."""
        assert config.detects_dialogs
        assert "position:fixed" in config.dialogs.detect_selector

    def test_the_maintenance_notice_is_deliberately_not_declared(self, config):
        """It is the trigger for the human-handoff demo. Declaring it as a known
        interstitial would make the escalation path undemonstrable."""
        assert config.dialogs.identify("Scheduled maintenance at 6 PM") is None

    def test_engine_defaults_apply(self, config):
        assert config.slow_load.max_attempts == 3
        # Bounded per attempt, not per step, and deliberately under the target
        # app's 8s stall: one long wait can only report that the page eventually
        # arrived, while a short wait plus retries reports that it was late and
        # by how much. Raise this above 8s and the SLOW_LOAD path stops being
        # reachable at all — the run goes green and records nothing.
        assert config.step_timeout_ms == 5_000

    def test_an_unset_timeout_falls_back_to_the_engine_default(self, tmp_path):
        assert RuntimeConfig.load("demo-app", write(tmp_path, """
            defaults: {}
            demo-app: {}
            """)).step_timeout_ms == 10_000


class TestSlowLoadBackoff:

    def test_backoff_grows_then_holds(self, config):
        policy = config.slow_load
        assert [policy.delay_before(n) for n in (1, 2, 3, 4)] == [1, 2, 4, 4]

    def test_no_backoff_configured_means_no_delay(self):
        assert SlowLoadPolicy(max_attempts=1, backoff_seconds=()).delay_before(1) == 0


class TestUnprofiledApp:

    def test_an_unknown_app_warns_rather_than_refuses(self):
        """Refusing would make a new app unreplayable until someone wrote a
        profile, and the strategy chain and checkpoints still work without one.
        Same posture as fingerprint drift: warn, proceed, say so."""
        config = RuntimeConfig.load("some-other-app")
        assert config.profiled is False
        assert config.app_error == ()
        assert config.session.recoverable is False

    def test_defaults_still_apply_without_a_profile(self):
        assert RuntimeConfig.load("some-other-app").slow_load.max_attempts == 3


class TestMalformedConfig:

    def test_a_broken_relogin_routine_is_caught_at_load(self, tmp_path):
        """Held to the artifact schema, because the same interpreter runs it. The
        alternative is finding out mid-recovery."""
        path = write(tmp_path, """
            defaults: {}
            demo-app:
              session_expired:
                relogin:
                  - id: 1
                    action: click
                    risk: read_only
            """)
        with pytest.raises(RuntimeConfigError, match="checkpoint"):
            RuntimeConfig.load("demo-app", path)

    def test_an_unknown_condition_kind_is_refused(self, tmp_path):
        path = write(tmp_path, """
            defaults: {}
            demo-app:
              app_error:
                - condition: dom_hash_equals
                  value: abc123
            """)
        with pytest.raises(RuntimeConfigError, match="unknown condition"):
            RuntimeConfig.load("demo-app", path)

    def test_a_malformed_condition_is_refused(self, tmp_path):
        path = write(tmp_path, """
            defaults: {}
            demo-app:
              app_error:
                - "HTTP 500"
            """)
        with pytest.raises(RuntimeConfigError, match="expected a condition"):
            RuntimeConfig.load("demo-app", path)

    def test_a_missing_file_is_refused(self, tmp_path):
        with pytest.raises(RuntimeConfigError, match="not found"):
            RuntimeConfig.load(APP, tmp_path / "nope.yaml")


class TestKnownInterstitials:

    def test_a_declared_interstitial_is_identified(self, tmp_path):
        path = write(tmp_path, """
            defaults: {}
            demo-app:
              dialogs:
                detect_selector: 'div.modal'
                known:
                  - name: SESSION_WARNING
                    detect:
                      condition: text_present
                      value: "Your session will expire"
                    dismiss:
                      strategies:
                        - kind: label
                          value: "Continue working"
                          role: button
            """)
        dialogs = RuntimeConfig.load("demo-app", path).dialogs
        found = dialogs.identify("Your session will expire in 5 minutes")
        assert found.name == "SESSION_WARNING"
        assert found.dismiss["strategies"][0]["value"] == "Continue working"

    def test_an_undeclared_dialog_stays_unknown(self, tmp_path):
        path = write(tmp_path, """
            defaults: {}
            demo-app:
              dialogs:
                detect_selector: 'div.modal'
                known:
                  - name: SESSION_WARNING
                    detect: { condition: text_present, value: "Your session will expire" }
            """)
        dialogs = RuntimeConfig.load("demo-app", path).dialogs
        assert dialogs.identify("Scheduled maintenance at 6 PM") is None
