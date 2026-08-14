"""Element distillation — turning a hostile page into a numbered list (DESIGN §3).

This is the part that has to work when the DOM is useless. The target app has no
ids, no data-* hooks, no `<label>` elements and no semantic markup, so the only
signal that names a form field is *the text in the cell next to it*. The label
derivation below encodes that: own text, then submit value, then the previous cell
in the same row, then the cell above in the previous row, and only then the
fallbacks a well-built page would have offered in the first place.

Two decisions worth stating:

**Text cells are collected, not just interactive controls.** The design describes
"a numbered list of interactive elements", but the `read` tool has to read a
savings balance, and a balance is not interactive. A list of only clickable things
would make `read` unusable and push extraction into screenshot OCR. So a cell with
text and no control inside it is an element too, labelled by its neighbour — which
is exactly what makes `label: "Savings balance"` a usable read target.

**Frames are traversed.** The profile page hides loan details in an unnamed
iframe. Anything that stops at the main document simply cannot see them, and the
whole point of the prop is that real legacy apps do this.
"""

# Runs inside the page. Returns plain JSON — no element handles cross the boundary,
# because a handle would go stale the moment the page changed and we want the
# recorded description to be the thing that travels, not a live pointer.
DISTILL_JS = r"""
() => {
  const MAX_TEXT = 120;

  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    if (r.bottom < 0 || r.right < 0) return false;
    const s = window.getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
  };

  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const textOf = (el) => clean(el.innerText || el.textContent || '');

  const roleOf = (el) => {
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'select') return 'select';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (t === 'submit' || t === 'button' || t === 'reset') return 'button';
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      if (t === 'password') return 'password';
      return 'textbox';
    }
    return 'control';
  };

  // Text sitting immediately after a control, up to the next control: the option
  // label in `<input type=radio ...>&nbsp;Mail`.
  const optionTextOf = (el) => {
    let node = el.nextSibling;
    let out = '';
    while (node && out.length < MAX_TEXT) {
      if (node.nodeType === 3) {
        out += node.textContent;
      } else if (node.nodeType === 1) {
        const tag = node.tagName.toLowerCase();
        if (tag === 'input' || tag === 'select' || tag === 'textarea') break;
        out += node.textContent || '';
      }
      node = node.nextSibling;
    }
    return clean(out);
  };

  // The legacy-surface heart of this file.
  const labelOf = (el) => {
    const role = roleOf(el);
    if (role === 'button') {
      const v = clean(el.getAttribute('value')) || textOf(el);
      if (v) return v;
    }
    // Own text names an element only where its visible content *is* its name:
    // links and buttons. For any other control the content is data, not a name —
    // a <select>'s text is its option list, so trusting it here labels the branch
    // picker "E14 — Eastern Main" instead of "Branch".
    if (role === 'link') {
      const own = textOf(el);
      if (own && own.length <= MAX_TEXT) return own;
    }

    // Radios and checkboxes are named by the text that trails them, not by the
    // cell they sit in: every radio in a group shares one cell, so the cell label
    // ("Statement delivery") is identical for all of them. Recording that would
    // give two options the same label and let replay resolve to whichever came
    // first — picking the wrong one and reporting success.
    if (role === 'radio' || role === 'checkbox') {
      const trailing = optionTextOf(el);
      if (trailing) return trailing;
    }

    const cell = el.closest('td, th');
    if (cell) {
      // 1. the previous cell in this row — the dominant legacy pattern
      let prev = cell.previousElementSibling;
      while (prev) {
        const t = textOf(prev);
        if (t && t.length <= MAX_TEXT) return t;
        prev = prev.previousElementSibling;
      }
      // 2. text sitting in the same cell alongside the control
      const beside = clean(cell.innerText || '');
      if (beside && beside.length <= MAX_TEXT) return beside;
      // 3. the cell directly above, for stacked layouts
      const row = cell.closest('tr');
      const prevRow = row && row.previousElementSibling;
      if (prevRow) {
        const column = Array.from(row.children).indexOf(cell);
        const above = prevRow.children[column];
        if (above) {
          const t = textOf(above);
          if (t && t.length <= MAX_TEXT) return t;
        }
      }
    }
    // Fallbacks a well-built page would have led with. Present for generality;
    // the target app deliberately offers none of them.
    return clean(el.getAttribute('aria-label'))
        || clean(el.getAttribute('placeholder'))
        || clean(el.getAttribute('title'))
        || clean(el.getAttribute('name'))
        || '';
  };

  const pathOf = (el) => {
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && node.tagName.toLowerCase() !== 'html') {
      const tag = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (parent) {
        const same = Array.from(parent.children).filter(c => c.tagName === node.tagName);
        parts.unshift(same.length > 1
          ? tag + ':nth-of-type(' + (same.indexOf(node) + 1) + ')'
          : tag);
      } else {
        parts.unshift(tag);
      }
      node = parent;
    }
    return parts.join(' > ');
  };

  const geometry = (el) => {
    const r = el.getBoundingClientRect();
    return {
      box: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
      center: [Math.round(r.x + r.width / 2), Math.round(r.y + r.height / 2)]
    };
  };

  const recordControl = (el) => {
    const role = roleOf(el);
    let text = '';
    // A password's value is never observed, so it can never be recorded, logged,
    // or screenshotted into a trace. Redaction starts here, at the point of
    // perception, rather than being filtered out later.
    if (role !== 'password' && el.value !== undefined && el.value !== null) {
      text = String(el.value).slice(0, MAX_TEXT);
    }
    // The owning form, when there is one. This is how risk gets classified
    // without guessing: a click that submits a POST is a mutation, and which
    // route it posts to decides whether it is merely mutating or irreversible.
    // Read from el.form rather than by walking ancestors, because the HTML parser
    // hoists <form> out of table structure — the form is often not an ancestor at
    // all, though the browser still knows it owns the control.
    let form = null;
    if (el.form) {
      const type = (el.getAttribute('type') || '').toLowerCase();
      const tag = el.tagName.toLowerCase();
      form = {
        method: (el.form.getAttribute('method') || 'get').toLowerCase(),
        action: el.form.getAttribute('action') || '',
        // Every control on a POST form belongs to that form, but only a submit
        // control submits it. Without this, clicking a dropdown reads as a
        // mutation, and a capability that reports five mutating steps when it has
        // one is a capability nobody reads carefully.
        submits: type === 'submit' || type === 'image'
                 || (tag === 'button' && type !== 'button' && type !== 'reset')
      };
    }
    return Object.assign({
      kind: 'interactive', role: role, label: labelOf(el),
      structural: pathOf(el), text: text, form: form
    }, geometry(el));
  };

  const recordCell = (el, label, text) => Object.assign({
    kind: 'text', role: 'cell', label: label,
    structural: pathOf(el), text: text.slice(0, MAX_TEXT), form: null
  }, geometry(el));

  const out = [];
  const CONTROLS = 'a[href], button, input, select, textarea, [onclick]';

  document.querySelectorAll(CONTROLS).forEach(el => {
    if ((el.getAttribute('type') || '').toLowerCase() === 'hidden') return;
    if (!visible(el)) return;
    out.push(recordControl(el));
  });

  // Text cells, walked a row at a time so a label/value pair collapses into ONE
  // element: the value is the thing worth reading, and the cell beside it is the
  // only thing that names it. Recording them separately produced elements that
  // read as `cell 'Member name' = 'Member name'` — the model could see a name and
  // a value but nothing connecting them. Here the element *is* the value, carries
  // the neighbour's text as its label, and its structural path points at the cell
  // that actually holds the data, so label and structural resolve to the same node.
  //
  // A cell with no value beside it (an error banner, a heading) is recorded on its
  // own terms, which is how "No member matches this number" reaches the agent.
  const OPAQUE = 'a[href], button, input, select, textarea, table';
  document.querySelectorAll('tr').forEach(row => {
    const cells = Array.from(row.children)
      .filter(c => c.tagName === 'TD' || c.tagName === 'TH');
    let i = 0;
    while (i < cells.length) {
      const cell = cells[i];
      const own = textOf(cell);
      if (!visible(cell) || cell.querySelector(OPAQUE) || !own || own.length > MAX_TEXT) {
        i += 1;
        continue;
      }
      let value = null;
      const next = cells[i + 1];
      if (next && visible(next) && !next.querySelector(OPAQUE)) {
        const t = textOf(next);
        if (t && t.length <= MAX_TEXT) value = next;
      }
      out.push(value ? recordCell(value, own, textOf(value))
                     : recordCell(cell, own, own));
      i += value ? 2 : 1;
    }
  });

  return out;
}
"""


def frame_label(frame, main_frame):
    """None for the main document, the frame URL otherwise."""
    return None if frame is main_frame else frame.url


def distill(page, max_elements=150):
    """Collect elements from the main document and every child frame, in order.

    Indices are assigned here rather than in the page so they remain stable across
    the frame walk. They are valid only for this observation — a step records
    strategies, never an index.
    """
    from .surface import Element

    collected = []
    for frame in page.frames:
        try:
            raw = frame.evaluate(DISTILL_JS)
        except Exception:
            # A frame can be mid-navigation or cross-origin. One unreadable frame
            # must not blind the agent to the rest of the page.
            continue
        where = frame_label(frame, page.main_frame)
        for item in raw:
            collected.append((item, where))

    elements = []
    for index, (item, where) in enumerate(collected[:max_elements]):
        elements.append(Element(
            index=index,
            kind=item["kind"],
            role=item["role"],
            label=item["label"],
            structural=item["structural"],
            center=tuple(item["center"]),
            box=tuple(item["box"]),
            text=item["text"],
            frame=where,
            form=item.get("form"),
        ))
    return elements
