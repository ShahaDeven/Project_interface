"""The seam between "how we perceive and act on a surface" and "the recorded flow".

Everything above this line — the discovery loop, the distiller, the replay
interpreter — is written against `Surface` and the two dataclasses here. Nothing
above it imports Playwright. That is the whole point: a desktop surface driven by
OS-level automation would implement the same three verbs and produce the same
`Element` records, and neither the agent loop nor a saved artifact would need to
change. The artifact records *what was targeted and how it was identified*, never
*which library did the clicking*.

An `Element` deliberately carries all three identification strategies at once —
label, structural path, and coordinates — because discovery is the only moment
they can all be observed together. Recording just the one that happened to work
would throw away the fallback chain that makes replay survive a layout edit.
"""

from dataclasses import dataclass, field
from typing import Optional, Protocol


@dataclass(frozen=True)
class Element:
    """One addressable thing on the surface, as observed at a single moment.

    `index` is stable only within the observation that produced it. A step never
    stores an index — it stores the strategies, which is what survives.
    """

    index: int
    kind: str                  # "interactive" | "text"
    role: str                  # textbox, button, link, select, checkbox, radio, cell...
    label: str                 # visible or adjacent text that names this thing
    structural: str            # CSS-ish path, exact today and brittle to layout edits
    center: tuple              # (x, y) viewport coordinates of the bounding-box centre
    box: tuple                 # (x, y, width, height)
    text: str = ""             # current value or cell content
    frame: Optional[str] = None  # frame URL; None means the main document
    form: Optional[dict] = None  # {method, action} of the owning form, for buttons

    def as_strategies(self):
        """The ordered fallback chain this element would be recorded with.

        Order is stability, not confidence: label text on a legacy surface outlives
        a table reshuffle, a structural path does not, and coordinates outlive
        neither — so coordinates come last and never travel without something to
        verify against.
        """
        strategies = []
        if self.label:
            strategies.append({"kind": "label", "value": self.label, "role": self.role})
        if self.structural:
            strategies.append({"kind": "structural", "value": self.structural})
        if self.label:
            strategies.append({
                "kind": "coordinates",
                "value": [self.center[0], self.center[1]],
                "verify_text_nearby": self.label,
            })
        return strategies

    def describe(self):
        """One line for the model. Terse on purpose: this is per-turn prompt cost."""
        where = f" [frame: {self.frame}]" if self.frame else ""
        content = f" = {self.text!r}" if self.text else ""
        return f"[{self.index}] {self.role} {self.label!r}{content}{where}"


@dataclass
class Observation:
    """What the agent sees on one turn: a screenshot plus a numbered element list."""

    url: str
    title: str
    elements: list = field(default_factory=list)
    screenshot_path: Optional[str] = None
    app_fingerprint: Optional[str] = None

    def render(self):
        """The textual half of the observation, as the model receives it."""
        lines = [f"URL: {self.url}", f"Title: {self.title}", "", "Elements:"]
        lines += ["  " + element.describe() for element in self.elements]
        return "\n".join(lines)

    def interactive(self):
        return [element for element in self.elements if element.kind == "interactive"]

    def by_index(self, index):
        for element in self.elements:
            if element.index == index:
                return element
        raise IndexError(
            f"no element with index {index}; the observation has "
            f"{len(self.elements)} element(s), 0-{len(self.elements) - 1}")


class Surface(Protocol):
    """The three verbs a surface must support, plus observation.

    Deliberately small. Every additional verb is a thing the artifact schema would
    have to be able to express and the replay engine would have to be able to
    repeat, so the bar for adding one is high.
    """

    def observe(self, screenshot_path=None) -> Observation: ...

    def current_url(self) -> str: ...

    def page_marker(self) -> Optional[str]: ...

    def text_present(self, needle: str) -> bool: ...

    def navigate(self, url: str) -> None: ...

    def click(self, element: Element) -> None: ...

    def type(self, element: Element, text: str) -> None: ...

    def read(self, element: Element) -> str: ...
