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

from ..policy import Allowlist, PolicyViolation
from .elements import distill
from .surface import Element, Observation

DEFAULT_TIMEOUT_MS = 10_000
SETTLE_TIMEOUT_MS = 5_000


class TargetNotFound(Exception):
    """A recorded target could not be resolved on the live page by any strategy."""


class BrowserSurface:
    """A live Chromium page, presented as a Surface.

    Headed by default: the human-handoff seam in §9 requires a window a person can
    actually take over, and a demo of a browser nobody can see proves less.
    """

    def __init__(self, playwright, allowlist=None, headless=False,
                 viewport=(1280, 900), timeout_ms=DEFAULT_TIMEOUT_MS):
        self.allowlist = allowlist or Allowlist.from_file()
        self.timeout_ms = timeout_ms
        self._browser = playwright.chromium.launch(headless=headless)
        self._context = self._browser.new_context(
            viewport={"width": viewport[0], "height": viewport[1]})
        self.page = self._context.new_page()
        self.page.set_default_timeout(timeout_ms)

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
        """Bounded wait for the page to stop moving. Never unbounded, never model-decided."""
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=SETTLE_TIMEOUT_MS)
            self.page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT_MS)
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

    def screenshot(self, path):
        from pathlib import Path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(path))
        return str(path)

    def text_present(self, needle):
        """Whole-document text search, frames included — a maintenance modal or a
        denial banner is just as real inside a frame as outside one.

        Whitespace-insensitive on both sides. The element list the agent reads from
        is already whitespace-normalised, while `inner_text` puts newlines between
        table cells, so a phrase copied faithfully out of the observation would
        otherwise fail to match the page it was copied from. Still a literal
        substring test: case and wording must be exact.
        """
        wanted = " ".join((needle or "").split())
        if not wanted:
            return False
        for frame in self.page.frames:
            try:
                body = frame.locator("body").inner_text(timeout=1000) or ""
            except Exception:
                continue
            if wanted in " ".join(body.split()):
                return True
        return False

    # -------------------------------------------------------------- actions --

    def navigate(self, url):
        self.allowlist.check_action("navigate")
        self.allowlist.check_navigate(url)
        self.page.goto(url, wait_until="domcontentloaded")
        self.settle()

    def click(self, element):
        self.allowlist.check_action("click")
        self._resolve(element).click()
        self.settle()

    def type(self, element, text):
        """Set a control's value.

        "Type" is the agent's only value-setting verb, so it has to mean the right
        thing for each kind of control rather than literally meaning keystrokes.
        A dropdown takes an option, a checkbox takes a state. Widening the action
        space instead — a `select` tool, a `check` tool — would put three more
        actions in the artifact schema and three more branches in the replay
        interpreter, to express something the surface already makes unambiguous.
        """
        self.allowlist.check_action("type")
        locator = self._resolve(element)

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

    def read(self, element):
        self.allowlist.check_action("read")
        locator = self._resolve(element)
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
