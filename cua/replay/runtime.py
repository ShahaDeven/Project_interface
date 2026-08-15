"""Loading the replay engine's runtime-condition recognisers (DESIGN §6).

Reads `runtime.yaml` into something the interpreter can consult after every step.
The file is documented at length; what lives here is the loading, the validation,
and two decisions the config format alone does not express.

**An unprofiled app warns rather than refuses.** Replaying against an app with no
entry here means the engine cannot tell a 500 page from a page that merely looks
wrong, so its forensics get less useful — but refusing outright would make a new
app unreplayable until someone wrote a profile, and the strategy chain plus
checkpoints still work fine without one. Same posture as fingerprint drift: warn,
proceed, and say so in the evidence.

**Recovery routines are validated as steps.** A `relogin` routine is executed by
the same interpreter as an artifact, so it is held to the same schema at load time
rather than discovered to be malformed halfway through a session recovery.
"""

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..contracts import ContractError, validate_steps

DEFAULT_RUNTIME_PATH = Path(__file__).resolve().parents[2] / "runtime.yaml"


class RuntimeConfigError(ValueError):
    """runtime.yaml is malformed. Raised at load time, never mid-run."""


@dataclass(frozen=True)
class SlowLoadPolicy:
    max_attempts: int = 3
    backoff_seconds: tuple = (1, 2, 4)

    def delay_before(self, attempt):
        """Seconds to wait before attempt N (1-based). Last value repeats."""
        if not self.backoff_seconds:
            return 0
        index = min(max(attempt - 1, 0), len(self.backoff_seconds) - 1)
        return self.backoff_seconds[index]


@dataclass(frozen=True)
class KnownInterstitial:
    name: str
    detect: dict
    dismiss: dict = None


@dataclass(frozen=True)
class DialogPolicy:
    detect_selector: str = ""
    known: tuple = ()

    def identify(self, text):
        """Which declared interstitial this dialog is, or None for an unknown one."""
        for interstitial in self.known:
            marker = (interstitial.detect or {}).get("value", "")
            if marker and marker in text:
                return interstitial
        return None


@dataclass(frozen=True)
class SessionPolicy:
    detect: tuple = ()
    relogin: tuple = ()

    @property
    def recoverable(self):
        """SESSION_EXPIRED is recoverable only if a routine exists to recover it."""
        return bool(self.relogin)


@dataclass
class RuntimeConfig:
    app: str
    profiled: bool = True
    step_timeout_ms: int = 10_000
    settle_timeout_ms: int = 5_000
    slow_load: SlowLoadPolicy = field(default_factory=SlowLoadPolicy)
    app_error: tuple = ()
    session: SessionPolicy = field(default_factory=SessionPolicy)
    dialogs: DialogPolicy = field(default_factory=DialogPolicy)

    @classmethod
    def load(cls, app, path=None):
        path = Path(path or DEFAULT_RUNTIME_PATH)
        if not path.exists():
            raise RuntimeConfigError(f"{path} not found")

        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        defaults = document.get("defaults", {}) or {}
        profile = document.get(app)

        slow = defaults.get("slow_load", {}) or {}
        config = cls(
            app=app,
            profiled=profile is not None,
            step_timeout_ms=int(defaults.get("step_timeout_ms", 10_000)),
            settle_timeout_ms=int(defaults.get("settle_timeout_ms", 5_000)),
            slow_load=SlowLoadPolicy(
                max_attempts=int(slow.get("max_attempts", 3)),
                backoff_seconds=tuple(slow.get("backoff_seconds", (1, 2, 4))),
            ),
        )
        if profile is None:
            return config

        config.app_error = tuple(_conditions(profile.get("app_error", []), "app_error"))

        session = profile.get("session_expired", {}) or {}
        relogin = list(session.get("relogin", []))
        if relogin:
            try:
                validate_steps(relogin, what=f"{app}/session_expired/relogin")
            except ContractError as error:
                raise RuntimeConfigError(str(error)) from None
        config.session = SessionPolicy(
            detect=tuple(_conditions(session.get("detect", []), "session_expired/detect")),
            relogin=tuple(relogin),
        )

        dialogs = profile.get("dialogs", {}) or {}
        config.dialogs = DialogPolicy(
            detect_selector=dialogs.get("detect_selector", ""),
            known=tuple(
                KnownInterstitial(
                    name=entry["name"],
                    detect=_condition(entry.get("detect"), f"dialogs/{entry.get('name')}"),
                    dismiss=entry.get("dismiss"),
                )
                for entry in dialogs.get("known", []) or []
            ),
        )
        return config

    @property
    def detects_dialogs(self):
        return bool(self.dialogs.detect_selector)


def _condition(raw, where):
    if not isinstance(raw, dict) or "condition" not in raw or "value" not in raw:
        raise RuntimeConfigError(
            f"{where}: expected a condition with 'condition' and 'value', got {raw!r}")
    if raw["condition"] not in ("text_present", "url_matches"):
        raise RuntimeConfigError(
            f"{where}: unknown condition {raw['condition']!r}; the engine can only "
            f"assert page text and landing URL")
    return dict(raw)


def _conditions(raw, where):
    return [_condition(item, where) for item in (raw or [])]
