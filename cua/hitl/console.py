"""The operator console: how a paused run reaches a person (DESIGN §9).

The console is deliberately mocked — a terminal prompt, not a web UI — and just as
deliberately an *interface* rather than a `print` and an `input`. Everything about
the handoff that can be wrong is on the resume path, and a handoff that can only be
exercised by someone sitting at a keyboard is a handoff that gets tested once, by
hand, the night before the demo. `ScriptedConsole` is what lets the state machine
have a test suite.

**Three attention channels, ranked by where the person is actually looking.** The
operator is watching the browser window, not the terminal, so a terminal-only
prompt is the weakest possible signal. In order:

1. The live page itself — a banner and a rewritten document title, painted by the
   surface (channel 1, and the one that does the real work). The title is what
   shows in the taskbar when the window is minimised.
2. This terminal block, which carries the detail and takes the decision.
3. The terminal bell, which is one real interrupt and costs nothing.

The failure mode being designed against is not "the operator says no". It is the
operator glancing at a finished-looking browser, assuming the run is over, and
closing the window.
"""

import getpass
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass

RESUME = "resume"
ABANDON = "abandon"

RULE = "═" * 68

# Sentinel: stdin ended. Distinct from "nothing typed yet", which is None.
_CLOSED = object()


@dataclass(frozen=True)
class InterventionRequest:
    """What the human is being asked, and about which step.

    `when` is load-bearing rather than descriptive, because the two cases resume
    differently. A risk gate pauses *before* the action, so resuming means run it
    now. A dialog or an expired session pauses *after* the action, so resuming
    means re-judge the page it landed on — the action is never re-sent. One resume
    path that does not know which case it is in is how a confirmation click posts
    twice.
    """

    run_id: str
    capability: str
    step: int
    steps_total: int
    reason: str
    detail: str
    requested_action: str
    screenshot: str = ""
    when: str = "after_step"

    @property
    def blocks_an_action(self):
        return self.when == "before_step"

    def headline(self):
        return "AUTOMATION PAUSED — YOUR ACTION IS NEEDED"

    def banner_choices(self):
        """The buttons the banner offers. Labelled by what they do, since the
        same key means different things either side of an action."""
        if self.blocks_an_action:
            return ((RESUME, "Approve and run this step"), (ABANDON, "Abandon run"))
        return ((RESUME, "Done — resume"), (ABANDON, "Abandon run"))

    def banner_lines(self):
        """The short form, for the page. Terse: it sits over the app the person
        has to work in, so it says what and where, and leaves why to the terminal.

        The instruction has to differ by `when`, and getting this wrong is worse
        than saying nothing. A gate pauses before the action: the operator is
        being asked for *permission*, and if they perform the click themselves the
        automation arrives at a page where its target no longer exists. Only an
        after_step pause is an invitation to touch the page.
        """
        if self.blocks_an_action:
            instruction = ("Do NOT do this yourself — approve in the terminal and "
                           "the automation performs it.")
        else:
            instruction = "Do what is needed here, then answer in the terminal."
        return [
            f"Step {self.step} of {self.steps_total} · {self.reason}",
            self.requested_action,
            instruction + " Closing this window ends the run.",
        ]

    def page_title(self):
        return f"[!] ACTION NEEDED - {self.capability}"


class TerminalConsole:
    """The real console: a block, a bell, and a blocking prompt.

    Refuses to pause when there is nobody to ask. Without that check a run in CI
    or behind a pipe hits `input()`, takes an immediate EOF, and reports itself
    abandoned by an operator who was never there.
    """

    poll_seconds = 0.25

    # Wait indefinitely by default: a paused run is waiting on a person, and there
    # is no defensible number of seconds after which their answer stops mattering —
    # least of all on the irreversible step, where giving up would mean abandoning a
    # half-finished mutation to save a process.
    #
    # Tests set it. A poll loop with no ceiling turns a channel that never delivers
    # into a *hang* rather than a failure, and a hung suite is worse than a red one:
    # no output, no traceback, indistinguishable from a slow browser test.
    deadline_seconds = None

    def __init__(self, stream=None, input_fn=None, stdin=None):
        self.stream = stream if stream is not None else sys.stdout
        self._input = input_fn or input
        self._stdin = stdin if stdin is not None else sys.stdin
        self._typed = None

    @property
    def operator(self):
        """Who answered, for the audit record.

        The local account, overridable by `CUA_OPERATOR`. Deliberately modest: it
        records who was *at the machine*, not an authenticated identity, and the
        record should not imply more assurance than the mechanism provides. Real
        attribution would come from whatever authenticates the operator console,
        which is the piece §9 deliberately mocks.
        """
        try:
            return os.environ.get("CUA_OPERATOR") or getpass.getuser()
        except Exception:
            return "unknown"

    def available(self):
        try:
            return bool(self._stdin.isatty())
        except Exception:
            return False

    def ask(self, request, watch=None):
        """Block until the operator decides, from either channel.

        `watch` is polled for a decision made on the page itself. Without it this
        is a plain blocking prompt — which is the whole of the behaviour when the
        surface cannot paint buttons, and is why the simple path is kept separate
        rather than everything routing through a poll loop.
        """
        self._render(request)
        return self._ask_typed() if watch is None else self._ask_either(watch)

    def _decode(self, answer):
        if answer in ("r", "resume", "y", "yes"):
            return RESUME
        if answer in ("a", "abandon", "n", "no"):
            return ABANDON
        return None

    def _ask_typed(self):
        while True:
            try:
                answer = (self._input("waiting > ") or "").strip().lower()
            except (EOFError, KeyboardInterrupt):
                # Ctrl-C at a handoff prompt means "I am not doing this", which is
                # a decision, not a crash.
                self._write("\n  abandoned.\n")
                return ABANDON
            decision = self._decode(answer)
            if decision:
                return decision
            self._write("  Answer 'r' to resume, or 'a' to abandon.\n")

    def _ask_either(self, watch):
        """Watch the page and the keyboard at once, first answer wins.

        The page is polled on this thread because a browser session belongs to the
        thread that made it; the keyboard is read on a background one. That way
        round rather than the reverse, and the reader is a single long-lived thread
        rather than one per pause — two threads racing for the same stdin would
        make the second handoff of a run read the first one's leftovers.
        """
        self._write("waiting (or use the buttons on the page) > ")
        started = time.monotonic()
        while True:
            try:
                if (self.deadline_seconds is not None
                        and time.monotonic() - started > self.deadline_seconds):
                    self._write(f"\n  nobody answered within {self.deadline_seconds}s "
                                f"— abandoned.\n")
                    return ABANDON

                decision = watch()
                if decision in (RESUME, ABANDON):
                    self._write(f"\n  {decision} — chosen on the page.\n")
                    return decision

                line = self._typed_line()
                if line is None:
                    time.sleep(self.poll_seconds)
                    continue
                if line is _CLOSED:
                    self._write("\n  abandoned.\n")
                    return ABANDON
                decision = self._decode(line.strip().lower())
                if decision:
                    return decision
                self._write("  Answer 'r' to resume, 'a' to abandon, or click above.\n"
                            "waiting > ")
            except KeyboardInterrupt:
                self._write("\n  abandoned.\n")
                return ABANDON

    def _typed_line(self):
        """Whatever the operator has typed since last asked, or None."""
        if self._typed is None:
            self._typed = queue.Queue()

            def pump():
                while True:
                    try:
                        self._typed.put(self._input(""))
                    except Exception:
                        self._typed.put(_CLOSED)
                        return

            threading.Thread(target=pump, daemon=True,
                             name="cua-operator-console").start()
        try:
            return self._typed.get_nowait()
        except queue.Empty:
            return None

    # ----------------------------------------------------------- rendering --

    def _write(self, text):
        try:
            self.stream.write(text)
            self.stream.flush()
        except Exception:
            pass

    def _render(self, request):
        if request.blocks_an_action:
            verb = ("This step has NOT run yet. Approving here is what runs it.\n"
                    "  Do NOT perform it yourself in the browser — the automation "
                    "would then\n  arrive at a page where its target no longer "
                    "exists.")
            choices = "  [r] approve and run this step    [a] abandon\n"
        else:
            verb = ("The step ran; the run is waiting on the page it landed on.\n"
                    "  Do what is needed in the browser, then answer here.")
            choices = "  [r] resume    [a] abandon\n"
        lines = [
            "\a\n",                       # the bell: one real interrupt, free
            RULE + "\n",
            f"  [!]  {request.headline()}\n",
            RULE + "\n",
            f"  run          {request.run_id}\n",
            f"  capability   {request.capability}   "
            f"step {request.step} of {request.steps_total}\n",
            f"  reason       {request.reason}\n",
            f"  detail       {request.detail}\n",
            f"  requested    {request.requested_action}\n",
        ]
        if request.screenshot:
            lines.append(f"  screenshot   {request.screenshot}\n")
        lines += [
            "\n",
            f"  {verb}\n",
            "  The browser window is LIVE. Do NOT close it.\n",
            "\n",
            choices,
            "─" * 68 + "\n",
        ]
        self._write("".join(lines))


class ScriptedConsole:
    """A console with no human behind it, for tests and unattended runs.

    `act` is the part that matters: it is called while control is HUMAN, with the
    live surface available to it, so a test can *do what the operator would do* —
    dismiss the modal, sign back in — and then answer. Without it a test can only
    prove the engine takes an answer, not that it re-judges the page afterwards,
    and re-judging is the whole safety property.
    """

    operator = "scripted"

    def __init__(self, decisions=RESUME, act=None):
        if isinstance(decisions, str):
            decisions = [decisions]
        self._decisions = list(decisions) or [ABANDON]
        self._act = act
        self.requests = []

    def available(self):
        return True

    def ask(self, request, watch=None):
        self.requests.append(request)
        if self._act:
            self._act(request)
        # The last answer repeats: a console that runs out of script should keep
        # behaving, not start throwing inside a recovery path.
        return self._decisions[min(len(self.requests), len(self._decisions)) - 1]
