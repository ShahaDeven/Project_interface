"""Redaction between raw observation and anything persisted (DESIGN §8).

Credentials are handled upstream and structurally — the model types a
`{secrets.*}` token and the value is substituted at the keystroke, so a password
never exists anywhere this module could see it. What is left is *business* data:
the balances and account values a `read` step extracts.

The rule from §8 is that those live in the result envelope, which is caller-bound,
and are masked in the step trace, which is debugging output that gets committed as
evidence and read by people who have no business need for the number. So the trace
records the *shape* of a value and a masked form of it; the value itself travels
only in the envelope.

The shape is not decoration: it is what lets the distiller declare an output's type
and parse mode without the trace ever holding the figure.
"""

import re

CURRENCY_RE = re.compile(r"^\$\s*[\d,]+(?:\.\d{1,2})?$|^[\d,]+\.\d{2}$")
INTEGER_RE = re.compile(r"^\d+$")
DECIMAL_RE = re.compile(r"^\d+\.\d+$")

MASKED_CURRENCY = "$•••••"
MAX_TRACE_TEXT = 60


def shape_of(value):
    """Classify an observed value. Drives output typing in the distiller."""
    text = (value or "").strip()
    if CURRENCY_RE.match(text):
        return "currency"
    if INTEGER_RE.match(text):
        return "integer"
    if DECIMAL_RE.match(text):
        return "number"
    return "text"


def mask(value):
    """The form of a value that is safe to persist in a step trace.

    Money is masked outright — it is the field most likely to be sensitive and
    least likely to be needed for debugging, since what you actually want to know
    from a trace is *that* a balance was read and from where. Other values are
    truncated rather than masked: a member name in a trace is how you tell a run
    that read the right record from one that read the wrong one, and the target app
    holds no real PII. A stricter policy is a change to this function, in one place.
    """
    text = (value or "").strip()
    if shape_of(text) == "currency":
        return MASKED_CURRENCY
    if len(text) > MAX_TRACE_TEXT:
        return text[:MAX_TRACE_TEXT] + "…"
    return text
