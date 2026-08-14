"""The agent's action space — six tools, and the reasons for the absences (§4).

What a tool records determines what an artifact can express, so these schemas are
upstream of everything: a field the agent never reports is a field the distiller
cannot template and replay cannot use.

**No `hover`** — statefulness for near-zero value on this surface.

**No `wait`** — waiting is executor policy. A model that can choose to wait will
wait instead of declaring itself stuck, and a capability that papers over a slow
page during discovery records that papering-over as if it were the flow.

**No `scroll`** — the executor resolves targets by strategy, not by what is
currently in the viewport, so scrolling is an implementation detail of clicking
rather than a decision the agent should be making.

`done` and `stuck` are loop control, not replayable actions, which is why the
artifact schema's step actions are only the other four.
"""

REASON = {
    "type": "string",
    "description": "Why this action, in one sentence. Recorded in the trace and "
                   "carried into the artifact for reviewability.",
}

CLICK = {
    "name": "click",
    "description": "Click one element from the numbered list in the latest observation.",
    "input_schema": {
        "type": "object",
        "properties": {
            "element_index": {"type": "integer", "description": "Index from the element list."},
            "reason": REASON,
        },
        "required": ["element_index", "reason"],
    },
}

TYPE = {
    "name": "type",
    "description": (
        "Type text into one element. For credentials, do NOT invent or guess a value: "
        "type the literal token {secrets.operator_id} or {secrets.operator_password}. "
        "The executor substitutes the real value at the keystroke; you never see it, "
        "and it is never recorded."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "element_index": {"type": "integer", "description": "Index from the element list."},
            "text": {
                "type": "string",
                "description": "Literal text to type, or a {secrets.*} token for credentials.",
            },
            "reason": REASON,
        },
        "required": ["element_index", "text", "reason"],
    },
}

NAVIGATE = {
    "name": "navigate",
    "description": (
        "Go directly to a URL. Checked against the allowlist first; a rejected "
        "navigation ends the run, so do not probe for what is permitted."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Absolute URL."},
            "reason": REASON,
        },
        "required": ["url", "reason"],
    },
}

READ = {
    "name": "read",
    "description": (
        "Extract the text of one element as a named output of this capability. "
        "Use this for every value the goal asks you to return."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "element_index": {"type": "integer", "description": "Index from the element list."},
            "label": {
                "type": "string",
                "description": "snake_case name for this output, e.g. savings_balance. "
                               "Becomes an output in the capability contract.",
            },
            "reason": REASON,
        },
        "required": ["element_index", "label", "reason"],
    },
}

DONE = {
    "name": "done",
    "description": (
        "The goal is complete. Only call this when the values you were asked for "
        "have been read and the page shows the end state."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "What was accomplished."},
            "outputs": {
                "type": "object",
                "description": "The values read, keyed by the labels given to `read`.",
            },
            "success_evidence": {
                "type": "string",
                "description": (
                    "A SHORT contiguous phrase copied character-for-character from the "
                    "element list — one label or one value, e.g. 'Member Profile' or "
                    "'$4,523.18'. Matched literally against the live page and kept as the "
                    "capability's permanent success checkpoint. Do NOT describe the page "
                    "or join several cells into a sentence; a description is rejected."
                ),
            },
        },
        "required": ["summary", "outputs", "success_evidence"],
    },
}

STUCK = {
    "name": "stuck",
    "description": (
        "You cannot safely proceed. Prefer this over guessing: an unexpected dialog, "
        "a screen you do not recognise, or an action you are not confident is correct "
        "are all reasons to hand over to a human."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "description": "Short reason code or phrase."},
            "blocker_description": {"type": "string", "description": "What is in the way."},
            "requested_action": {
                "type": "string",
                "description": "What a human operator should do, and what state means it is resolved.",
            },
        },
        "required": ["reason", "blocker_description", "requested_action"],
    },
}

ALL_TOOLS = [CLICK, TYPE, NAVIGATE, READ, DONE, STUCK]

ACTION_TOOLS = {"click", "type", "navigate", "read"}
TERMINAL_TOOLS = {"done", "stuck"}
