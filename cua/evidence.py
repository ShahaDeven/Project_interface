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

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = REPO_ROOT / "evidence"


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

    def relative(self, path):
        """A path as it should be *recorded*: repo-relative and forward-slashed.

        Every path that reaches a result envelope or a trace goes through here.
        Screenshots and DOM snapshots are written with absolute paths — they have
        to be, or writing them would depend on the working directory — but an
        absolute path in a committed envelope is both a leak of the author's
        machine and useless to the reviewer reading it.

        Relative to the repo rather than to `cwd`, so the value does not change
        with where the command was run from. A run whose evidence lives outside
        the repo keeps its absolute path, because a relative one would then be
        relative to nothing the reader has; it is still forward-slashed, since a
        backslash in JSON is an escape character before it is a path separator.
        """
        candidate = Path(path)
        try:
            return candidate.resolve().relative_to(REPO_ROOT).as_posix()
        except ValueError:
            return candidate.as_posix()

    @property
    def path(self):
        """Repo-relative, forward-slashed — this string goes into result envelopes
        and should not carry a Windows drive letter into a committed artifact."""
        return self.relative(self.dir) + "/"

    def read_trace(self):
        if not self._trace_path.exists():
            return []
        with open(self._trace_path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]
