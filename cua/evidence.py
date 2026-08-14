"""Per-run evidence directories (DESIGN §10).

Shared by discovery and replay, because a run is a run: the same trace format, the
same screenshot conventions, the same result envelope. A reviewer comparing a
discovery run against a replay of the artifact it produced should be reading two
files with the same shape, not two formats that happen to describe similar things.

    evidence/run_<id>/
      trace.jsonl        one JSON object per event, append-only
      result.json        the run's result envelope (§7)
      screenshots/
      transcript.jsonl   discovery only: every model request and response

`trace.jsonl` is line-delimited on purpose. A run that dies mid-step still leaves a
readable trace up to the point it died, which is exactly when you most want one.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "evidence"


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_run_id(when=None):
    when = when or datetime.now()
    return "run_" + when.strftime("%Y%m%d_%H%M%S")


class RunEvidence:

    def __init__(self, run_id=None, root=None):
        self.run_id = run_id or new_run_id()
        self.dir = Path(root or DEFAULT_ROOT) / self.run_id
        self.screenshots = self.dir / "screenshots"
        self.screenshots.mkdir(parents=True, exist_ok=True)
        self._trace_path = self.dir / "trace.jsonl"
        self._transcript_path = self.dir / "transcript.jsonl"

    # ------------------------------------------------------------- writing --

    def _append(self, path, record):
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return record

    def trace(self, event, **fields):
        """Append one step-trace event. Timestamped here so callers cannot forget."""
        return self._append(self._trace_path, dict(event=event, at=utc_now(), **fields))

    def transcript(self, direction, payload):
        """Every model request and response, verbatim. Discovery only.

        The brief asks for evidence that the discovery run was real; a transcript
        is the only thing that actually shows it.
        """
        return self._append(self._transcript_path,
                            {"direction": direction, "at": utc_now(), "payload": payload})

    def screenshot_path(self, name):
        return self.screenshots / (name if name.endswith(".png") else f"{name}.png")

    def write_json(self, name, payload):
        path = self.dir / name
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False, default=str)
            handle.write("\n")
        return path

    def write_text(self, name, text):
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    # ------------------------------------------------------------- reading --

    @property
    def path(self):
        """Repo-relative, forward-slashed — this string goes into result envelopes
        and should not carry a Windows drive letter into a committed artifact."""
        try:
            relative = self.dir.resolve().relative_to(Path.cwd().resolve())
        except ValueError:
            relative = self.dir
        return relative.as_posix() + "/"

    def read_trace(self):
        if not self._trace_path.exists():
            return []
        with open(self._trace_path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]
