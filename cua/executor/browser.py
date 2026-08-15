"""Playwright implementation of `Surface` (DESIGN §3, §8).

The only module in the system that imports Playwright. Everything above it talks
to `Surface`.

Two policies live here rather than anywhere else:

**Waiting.** Every action is followed by a bounded readiness wait — the page has
settled and the DOM is quiet. It is executor policy precisely so the model cannot
choose it: §4 deliberately gives the agent no `wait` tool, because an agent that
can wait will paper over a broken step instead of declaring it stuck.

**The allowlist.** Checked before the action reaches the browser, not audited
after. A rejected action never happens.
"""

import time

from playwright.sync_api import TimeoutError as PlaywrightTimeout

from ..policy import Allowlist, PolicyViolation
from .elements import distill
from .surface import Element, Observation

DEFAULT_TIMEOUT_MS = 10_000
SETTLE_TIMEOUT_MS = 5_000

# Marks anything this system paints onto the page, so the system can tell its own
# furniture from the application's. Exactly one thing is painted — the handoff
# banner — and it must be excluded from dialog detection, because it is a
# fixed-position element and that is precisely how runtime.yaml recognises a modal.
BANNER_MARKER = "data-cua-handoff"

_SHOW_BANNER = """
(banner) => {
  document.querySelectorAll('[data-cua-handoff]').forEach(n => n.remove());
  if (!document.body) return;
  const box = document.createElement('div');
  box.setAttribute('data-cua-handoff', 'banner');
  box.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;'
    + 'background:#7a0000;color:#ffffff;padding:10px 14px;'
    + 'border-bottom:4px solid #ffcc00;text-align:left;'
    + 'font:bold 13px Verdana,Arial,sans-serif;';
  const head = document.createElement('div');
  head.style.cssText = 'font-size:16px;letter-spacing:0.5px;';
  head.textContent = banner.headline;
  box.appendChild(head);
  for (const line of banner.lines) {
    const row = document.createElement('div');
    row.style.cssText = 'font-weight:normal;font-size:12px;padding-top:3px;';
    row.textContent = line;
    box.appendChild(row);
  }
  if (banner.choices && banner.choices.length) {
    const bar = document.createElement('div');
    bar.style.cssText = 'padding-top:8px;';
    for (const choice of banner.choices) {
      const button = document.createElement('button');
      button.setAttribute('data-cua-handoff', 'choice');
      button.textContent = choice.label;
      button.style.cssText = 'margin-right:8px;padding:6px 14px;cursor:pointer;'
        + 'font:bold 13px Verdana,Arial,sans-serif;border:2px solid #ffffff;'
        + 'background:' + (choice.value === 'resume' ? '#0b6b2f' : '#333333')
        + ';color:#ffffff;';
      button.onclick = () => {
        document.documentElement.setAttribute('data-cua-decision', choice.value);
        bar.textContent = 'Sent: ' + choice.label + '. Returning control...';
        bar.style.cssText = 'padding-top:8px;font-weight:normal;font-size:12px;';
      };
      bar.appendChild(button);
    }
    box.appendChild(bar);
  }
  document.body.appendChild(box);
  if (banner.title) {
    const root = document.documentElement;
    if (!root.hasAttribute('data-cua-title')) {
      root.setAttribute('data-cua-title', document.title);
    }
    document.title = banner.title;
  }
}
"""

_CLEAR_BANNER = """
() => {
  document.querySelectorAll('[data-cua-handoff]').forEach(n => n.remove());
  const root = document.documentElement;
  root.removeAttribute('data-cua-decision');
  const prior = root.getAttribute('data-cua-title');
  if (prior !== null) {
    document.title = prior;
    root.removeAttribute('data-cua-title');
  }
}
"""

# Read on a poll while the operator holds control. An attribute on <html> rather
# than a JS variable because the banner is repainted after navigation, and a
# variable would not survive the very page change an operator is most likely to
# cause.
_READ_DECISION = "() => document.documentElement.getAttribute('data-cua-decision')"


class TargetNotFound(Exception):
    """A recorded target could not be resolved on the live page by any strategy."""


class ActionTimeout(Exception):
    """An action did not complete within the per-attempt bound.

    Deliberately distinct from a failure. The action may well have gone through —
    a submitted form can be processing while the browser gives up waiting — so the
    caller's job is to wait and re-check, never to re-send. Retrying the action
    itself is how automation double-posts.
    """


class BrowserSurface:
    """A live Chromium page, presented as a Surface.

    Headed by default: the human-handoff seam in §9 requires a window a person can
    actually take over, and a demo of a browser nobody can see proves less.
    """

    def __init__(self, playwright, allowlist=None, headless=False,
                 viewport=(1280, 900), timeout_ms=DEFAULT_TIMEOUT_MS,
                 settle_timeout_ms=SETTLE_TIMEOUT_MS):
        self.allowlist = allowlist or Allowlist.from_file()
        self.timeout_ms = timeout_ms
        self.settle_timeout_ms = settle_timeout_ms
        self._browser = playwright.chromium.launch(headless=headless)
        self._context = self._browser.new_context(
            viewport={"width": viewport[0], "height": viewport[1]})
        self.page = self._context.new_page()
        self.page.set_default_timeout(timeout_ms)
        self._banner = None
        # A navigation wipes the banner, and a paused run is exactly when the human
        # is navigating. Repainting on load keeps the notice attached to the
        # session rather than to one document.
        self.page.on("load", lambda *_: self._paint_banner())

    # ----------------------------------------------------------- lifecycle --

    def close(self):
        for closer in (self._context.close, self._browser.close):
            try:
                closer()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    @property
    def session_id(self):
        """Identifies the live session a human would take over during handoff."""
        return f"browser_sess_{id(self._context):x}"

    # ---------------------------------------------------------- perception --

    def settle(self):
        """Bounded wait for the page to stop moving. Never unbounded, never model-decided.

        Bounded separately from the action timeout because they answer different
        questions: `timeout_ms` is how long one action may take to land, this is
        how long the page may take to stop moving afterwards. A surface with slow
        polling widgets wants the second raised without loosening the first.
        """
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=self.settle_timeout_ms)
            self.page.wait_for_load_state("networkidle", timeout=self.settle_timeout_ms)
        except Exception:
            # A page that never goes idle is a condition to observe and report,
            # not a reason to hang. The outcome scanner deals with what it finds.
            pass

    def observe(self, screenshot_path=None):
        self.settle()
        if screenshot_path:
            self.screenshot(screenshot_path)
        return Observation(
            url=self.page.url,
            title=self.page.title(),
            elements=distill(self.page),
            screenshot_path=str(screenshot_path) if screenshot_path else None,
            app_fingerprint=self.app_fingerprint(),
        )

    def app_fingerprint(self):
        """What the app says it is. None when it says nothing — a crashed page
        reports no build, and 'unknown' must never be read as 'unchanged'."""
        try:
            return self.page.get_attribute('meta[name="generator"]', "content", timeout=500)
        except Exception:
            return None

    def current_url(self):
        return self.page.url

    def background_request(self, url):
        """Issue a same-session request without navigating away from the page.

        Used only to arm the target app's chaos flags, which are session-scoped:
        the browser that will experience the condition has to be the one that arms
        it, and a navigation would destroy the page state the flow has built up.
        Still allowlisted — a background request is an action like any other.
        """
        self.allowlist.check_navigate(url)
        return self.page.evaluate(
            # Redirects are NOT followed. A followed redirect is a second request,
            # and a second request is another page load — which, when this is used
            # to arm a one-shot condition, means the arming call consumes the very
            # thing it just armed.
            "async (target) => (await fetch(target, "
            "{credentials: 'same-origin', redirect: 'manual'})).status",
            url)

    def dialog_present(self, selector):
        """Text of any visible modal, or None when there is none.

        Takes a selector because there is no portable way to ask a page whether
        something is covering it — no accessibility tree here, no convention. How
        this app renders a modal is declared in runtime.yaml; the engine's rule on
        top of it is structural: anything matching that is not declared is unknown,
        and unknown means stop.

        The combined text of every match is returned, because a modal is usually
        two elements — a dimming overlay carrying no text and the box carrying all
        of it — and either alone is a misleading answer.
        """
        if not selector:
            return None
        try:
            found = self.page.locator(selector)
            texts = []
            for index in range(found.count()):
                item = found.nth(index)
                if item.get_attribute(BANNER_MARKER) is not None:
                    # Our own handoff banner. It matches this selector by
                    # construction — it is fixed-position, which is how a modal is
                    # recognised here — and reporting it would pause the run a
                    # second time over a notice the engine painted itself.
                    continue
                if item.is_visible():
                    texts.append((item.inner_text() or "").strip())
            if not texts:
                return None
            return " ".join(" ".join(texts).split())
        except Exception:
            return None

    def page_marker(self):
        """The shortest distinctive text that names the page we are now on.

        Used to synthesise per-step checkpoints at distillation time. Takes the
        leading segment of the document title ("Member Profile — Demo Credit
        Union" -> "Member Profile") but only returns it after confirming it is
        really in the body: a title is not visible text, and a checkpoint that
        asserts something only present in <head> would never fire at replay.
        Returns None when nothing qualifies, so the distiller can fall back rather
        than record a checkpoint that cannot pass.
        """
        try:
            title = self.page.title() or ""
        except Exception:
            return None
        for separator in (" — ", " - ", " | "):
            if separator in title:
                title = title.split(separator)[0]
                break
        title = title.strip()
        return title if title and self.text_present(title) else None

    # --------------------------------------------------------- handoff UI --

    def show_banner(self, headline, lines=(), title=None, choices=()):
        """Paint the handoff notice onto the live page (DESIGN §9, channel 1).

        The operator is looking at the browser, not at the terminal, so this is
        the channel that does the real work — and the rewritten document title is
        the half that survives the window being minimised, since that is what the
        taskbar shows.

        Held on the surface rather than written once, because a navigation
        destroys it and a paused run is precisely when someone is navigating.

        `choices` puts the handback where the operator's hands already are. That
        is not a convenience: the first human run of this seam ended with the
        operator pressing the application's own confirm button, because they were
        in a browser and had just been asked to confirm something. Giving them the
        right thing to click is a better answer than telling them not to click the
        wrong one.
        """
        self._banner = {"headline": headline, "lines": list(lines), "title": title,
                        "choices": [{"value": value, "label": label}
                                    for value, label in choices]}
        self._paint_banner()

    def banner_decision(self):
        """Which banner button the operator pressed, or None. Polled, not awaited:
        the console has to stay responsive to the terminal at the same time."""
        try:
            return self.page.evaluate(_READ_DECISION)
        except Exception:
            return None

    def clear_banner(self):
        """Take it down and restore the page's own title.

        Called before the engine looks at the page again, never merely at the end
        of the run: the banner is fixed-position furniture, and a modal detector
        that finds it would report an unknown dialog and pause over our own notice.
        """
        self._banner = None
        try:
            self.page.evaluate(_CLEAR_BANNER)
        except Exception:
            # A closed or navigating page has nothing to clean up, and failing to
            # remove a banner is never worth ending a run over.
            pass

    def _paint_banner(self):
        if not self._banner:
            return
        try:
            self.page.evaluate(_SHOW_BANNER, self._banner)
        except Exception:
            pass

    def screenshot(self, path):
        from pathlib import Path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(path))
        return str(path)

    def dom_snapshot(self, path):
        """Serialised DOM at a moment of failure (§7, §10).

        A screenshot shows what it looked like; this shows what it *was*. When a
        recorded locator stops resolving, the difference between "the element
        moved" and "the page was never rendered" is only visible here.
        """
        from pathlib import Path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(self.page.content(), encoding="utf-8")
        return str(path)

    def page_text(self):
        """All visible text, frames included, whitespace-normalised.

        Returned in one call so a caller checking many needles — the outcome
        scanner checks every declared outcome after every step — pays for one DOM
        read rather than one per needle. A denial banner or a maintenance modal is
        just as real inside a frame as outside one.
        """
        chunks = []
        for frame in self.page.frames:
            try:
                chunks.append(frame.locator("body").inner_text(timeout=1000) or "")
            except Exception:
                continue
        return " ".join(" ".join(chunks).split())

    def text_present(self, needle, haystack=None):
        """Literal substring test, whitespace-insensitive on both sides.

        The element list the agent reads from is already whitespace-normalised,
        while `inner_text` puts newlines between table cells, so a phrase copied
        faithfully out of an observation would otherwise fail to match the page it
        came from. Case and wording still have to be exact.
        """
        wanted = " ".join((needle or "").split())
        if not wanted:
            return False
        return wanted in (self.page_text() if haystack is None else haystack)

    # -------------------------------------------------------------- actions --

    def set_timeout(self, milliseconds, settle_ms=None):
        """Per-attempt bound, supplied by the replay engine's runtime config.

        `settle_ms` is optional so discovery, which has no runtime profile to read
        one from, keeps the built-in default.
        """
        self.timeout_ms = milliseconds
        self.page.set_default_timeout(milliseconds)
        if settle_ms is not None:
            self.settle_timeout_ms = settle_ms

    def navigate(self, url):
        self.allowlist.check_action("navigate")
        self.allowlist.check_navigate(url)
        try:
            self.page.goto(url, wait_until="domcontentloaded")
        except PlaywrightTimeout:
            raise ActionTimeout(f"navigation to {url} exceeded {self.timeout_ms}ms") from None
        self.settle()

    def click(self, element, strategies=None):
        self.allowlist.check_action("click")
        locator = self._resolve(element, strategies)
        try:
            locator.click()
        except PlaywrightTimeout:
            raise ActionTimeout(
                f"click on {element.label!r} exceeded {self.timeout_ms}ms") from None
        self.settle()

    def type(self, element, text, strategies=None):
        """Set a control's value.

        "Type" is the agent's only value-setting verb, so it has to mean the right
        thing for each kind of control rather than literally meaning keystrokes.
        A dropdown takes an option, a checkbox takes a state. Widening the action
        space instead — a `select` tool, a `check` tool — would put three more
        actions in the artifact schema and three more branches in the replay
        interpreter, to express something the surface already makes unambiguous.
        """
        self.allowlist.check_action("type")
        locator = self._resolve(element, strategies)

        if element.role == "select":
            try:
                locator.select_option(label=text)
            except Exception:
                # Legacy selects often carry codes as values and prose as labels
                # ("E14" / "E14 — Eastern Main"); try the other side before failing.
                locator.select_option(value=text)
            return

        if element.role in ("checkbox", "radio"):
            if text.strip().lower() in ("", "no", "false", "off", "unchecked"):
                locator.uncheck()
            else:
                locator.check()
            return

        locator.fill("")
        locator.fill(text)

    def read(self, element, strategies=None):
        self.allowlist.check_action("read")
        locator = self._resolve(element, strategies)
        if element.kind == "interactive" and element.role != "password":
            value = locator.input_value() if element.role in ("textbox", "select") else None
            if value:
                return value.strip()
        return (locator.inner_text() or "").strip()

    # ------------------------------------------------------------ targeting --

    def _frame_for(self, element):
        if not element.frame:
            return self.page.main_frame
        for frame in self.page.frames:
            if frame.url == element.frame:
                return frame
        raise TargetNotFound(f"frame '{element.frame}' is no longer on the page")

    def _resolve(self, element, strategies=None):
        """Resolve an element to a live locator, trying strategies in order.

        Returns the locator and records which strategy won on `self.last_strategy`
        — drift telemetry for free, and the thing replay logs per step.
        """
        frame = self._frame_for(element)
        attempts = []

        for strategy in (strategies or element.as_strategies()):
            kind = strategy["kind"]
            try:
                locator = self._locate(frame, kind, strategy, element)
                if locator is not None and locator.count() > 0:
                    self.last_strategy = f"{kind}: {strategy['value']}"
                    return locator.first
                attempts.append(f"{kind}: {strategy['value']} (no match)")
            except Exception as error:
                attempts.append(f"{kind}: {strategy['value']} ({type(error).__name__})")

        raise TargetNotFound(
            f"could not resolve {element.role} {element.label!r}; tried " + "; ".join(attempts))

    def _locate(self, frame, kind, strategy, element):
        if kind == "structural":
            return frame.locator(strategy["value"])

        if kind == "label":
            return self._locate_by_label(frame, strategy["value"], element)

        if kind == "coordinates":
            # Never click blind: the recorded text must still be near the point
            # before the point is trusted.
            x, y = strategy["value"]
            if not self._text_near(frame, x, y, strategy["verify_text_nearby"]):
                return None
            return frame.locator(self._selector_at(frame, x, y))

        return None

    def _locate_by_label(self, frame, label, element):
        """Find a control by the text that names it on a legacy surface.

        Tries the element's own text first (buttons, links carry their label), then
        the adjacent-cell relationship that the markup actually uses.
        """
        role = element.role
        if role == "button":
            candidate = frame.locator(
                f'input[type=submit][value="{label}"], input[type=button][value="{label}"], button')
            exact = frame.locator(f'input[value="{label}"]')
            if exact.count() > 0:
                return exact
            return candidate.filter(has_text=label) if candidate.count() else None
        if role == "link":
            return frame.get_by_role("link", name=label, exact=False)
        if role in ("radio", "checkbox"):
            # Matched on the text trailing the input, which is what named it at
            # distillation time. `contains` rather than equality because the gap is
            # usually a non-breaking space, which XPath's normalize-space leaves in
            # place.
            return frame.locator(
                f'xpath=//input[@type="{role}"]'
                f'[contains(following-sibling::text()[1], "{label}")]')
        if element.kind == "text":
            # A data cell: the value sits in the cell after the one holding the label.
            return frame.locator(
                f'xpath=//td[normalize-space(.)="{label}"]/following-sibling::td[1]'
                f' | //th[normalize-space(.)="{label}"]/following-sibling::td[1]')
        # A form control labelled by the previous cell.
        return frame.locator(
            f'xpath=//td[normalize-space(.)="{label}"]/following-sibling::td[1]'
            f'//*[self::input or self::select or self::textarea]')

    def _text_near(self, frame, x, y, needle, radius=140):
        """Is the recorded nearby text still around this point?"""
        try:
            found = frame.evaluate(
                """([x, y, radius, needle]) => {
                    const els = document.elementsFromPoint(x, y);
                    for (const el of els) {
                      const t = (el.innerText || el.textContent || '');
                      if (t && t.indexOf(needle) !== -1) return true;
                    }
                    const all = document.querySelectorAll('td, th, label, span, div, b, font');
                    for (const el of all) {
                      const t = (el.innerText || el.textContent || '').trim();
                      if (!t || t.indexOf(needle) === -1) continue;
                      const r = el.getBoundingClientRect();
                      const cx = r.x + r.width / 2, cy = r.y + r.height / 2;
                      if (Math.hypot(cx - x, cy - y) <= radius) return true;
                    }
                    return false;
                }""",
                [x, y, radius, needle])
            return bool(found)
        except Exception:
            return False

    def _selector_at(self, frame, x, y):
        """CSS path of whatever is at this point, so the click lands on an element
        rather than on a coordinate."""
        return frame.evaluate(
            """([x, y]) => {
                const el = document.elementFromPoint(x, y);
                if (!el) return 'body';
                const parts = [];
                let node = el;
                while (node && node.nodeType === 1 && node.tagName.toLowerCase() !== 'html') {
                  const tag = node.tagName.toLowerCase();
                  const parent = node.parentElement;
                  if (parent) {
                    const same = Array.from(parent.children).filter(c => c.tagName === node.tagName);
                    parts.unshift(same.length > 1
                      ? tag + ':nth-of-type(' + (same.indexOf(node) + 1) + ')' : tag);
                  } else parts.unshift(tag);
                  node = parent;
                }
                return parts.join(' > ');
            }""",
            [x, y])


def elapsed_ms(started):
    return int((time.monotonic() - started) * 1000)
