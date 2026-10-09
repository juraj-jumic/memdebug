"""A before/after view that shows what changed in a text, the way a code review would.

Texts are compared line by line, because a memory is usually a few lines or a whole markdown file. For a line
that was edited, the answer depends on how much of it survived:

* Mostly kept (one word swapped, a sentence appended): the line is shown once with the changed words marked,
  which is what someone looking for a planted instruction needs.
* Rewritten (little in common, or the word-by-word diff would be a scatter of fragments): the old line is shown
  struck out and the new line below it. Marking words inside a rewrite only produces confetti.

Long texts keep two unchanged lines either side of each change and fold the rest into one quiet line, so the
changes stay on screen. The full text is always available separately.

Every piece of text goes through block(), so it is escaped and control characters are made visible exactly
as everywhere else in the viewer.
"""
from __future__ import annotations

import difflib
import re

from .html import Markup, block, el

__all__ = ["redline", "change_snippet", "MAX_REDLINE_CHARS", "MAX_DOCUMENT_CHARS", "MAX_DOCUMENT_LINES", "CONTEXT_LINES",
           "MAX_SNIPPET_CHARS"]

MAX_REDLINE_CHARS = 3000      # up to this size a text is shown whole; longer texts fold their unchanged stretches
MAX_DOCUMENT_CHARS = 400_000  # beyond these the comparison would be slow; the caller falls back
MAX_DOCUMENT_LINES = 6000
MAX_SNIPPET_CHARS = 20_000    # a single line longer than this is never compared word by word
CONTEXT_LINES = 2             # unchanged lines kept on each side of a change
MAX_INLINE_SEGMENTS = 4       # more separate edits than this in one line and it reads as a rewrite
_TOKENS = re.compile(r"\s+|\w+|[^\w\s]", re.UNICODE)


# -- word level -------------------------------------------------------------------------------------------------

def _opcodes(old_tokens: list[str], new_tokens: list[str]) -> list[tuple]:
    """Word-level opcodes where two changes separated only by spaces count as one change.

    Without this, "likes tea" becoming "ignore all previous instructions" is reported as two edits with a
    space between them.
    """
    ops = difflib.SequenceMatcher(None, old_tokens, new_tokens, autojunk=False).get_opcodes()
    merged: list[tuple] = []
    k = 0
    while k < len(ops):
        tag, i1, i2, _, _ = ops[k]
        if (tag == "equal" and merged and merged[-1][0] != "equal" and k + 1 < len(ops) and ops[k + 1][0] != "equal"
                and all(token.isspace() for token in old_tokens[i1:i2])):
            before, after = merged.pop(), ops[k + 1]
            merged.append(("replace", before[1], after[2], before[3], after[4]))
            k += 2
            continue
        merged.append(ops[k])
        k += 1
    return merged


def _words_count(tokens: list[str]) -> int:
    return sum(1 for token in tokens if not token.isspace())


def _inline_reads_well(ops: list[tuple], old_tokens: list[str], new_tokens: list[str]) -> bool:
    """Would marking the changed words inside the line be easy to read?"""
    total = _words_count(old_tokens) + _words_count(new_tokens)
    if total == 0:
        return True
    kept = 2 * sum(_words_count(old_tokens[i1:i2]) for tag, i1, i2, _, _ in ops if tag == "equal") / total
    segments = sum(1 for op in ops if op[0] != "equal")
    if kept < 0.2:  # almost nothing in common: only tiny texts are clearer inline
        return len("".join(old_tokens)) + len("".join(new_tokens)) <= 60
    return segments <= MAX_INLINE_SEGMENTS or kept >= 0.75


def _marked(text: str, kind: str) -> list[Markup]:
    """Highlight the words of a changed run but leave the spaces around them unmarked."""
    if not text:
        return []
    core = text.strip()
    if not core:
        return [block(text)]
    start = text.index(core)
    lead, trail = text[:start], text[start + len(core):]
    return [piece for piece in (block(lead) if lead else None, el("span", block(core), class_=kind),
                                block(trail) if trail else None) if piece is not None]


def _plan(old_line: str, new_line: str) -> tuple[list[tuple], list[str], list[str]] | None:
    """The word-level comparison of two lines, or None when it should be shown as a rewrite instead."""
    if len(old_line) > MAX_SNIPPET_CHARS or len(new_line) > MAX_SNIPPET_CHARS:
        return None
    old_tokens, new_tokens = _TOKENS.findall(old_line), _TOKENS.findall(new_line)
    ops = _opcodes(old_tokens, new_tokens)
    return (ops, old_tokens, new_tokens) if _inline_reads_well(ops, old_tokens, new_tokens) else None


def _inline_pieces(ops: list[tuple], old_tokens: list[str], new_tokens: list[str]) -> list[Markup]:
    pieces: list[Markup] = []
    for tag, i1, i2, j1, j2 in ops:
        old, new = "".join(old_tokens[i1:i2]), "".join(new_tokens[j1:j2])
        if tag == "equal":
            pieces.append(block(old))
            continue
        pieces += _marked(old, "rm")
        pieces += _marked(new, "ins")
    return pieces


# -- line level -------------------------------------------------------------------------------------------------

def _line(*pieces: Markup, kind: str | None = None) -> Markup:
    """One line. `kind` is "gone" or "added" for a line that was removed or added as a whole."""
    return el("div", *pieces, class_="ln" if kind is None else f"ln {kind}")


def _fold(count: int) -> Markup:
    return el("div", f"{count} unchanged line{'' if count == 1 else 's'}", class_="fold")


def _split(before: str, after: str) -> tuple[list[str], list[str]]:
    old = before.replace("\r\n", "\n").split("\n")
    new = after.replace("\r\n", "\n").split("\n")
    if len(old) > 1 and len(new) > 1 and old[-1] == "" and new[-1] == "":  # both end with a line break: not a change
        old.pop()
        new.pop()
    return old, new


def _pair(old_line: str, new_line: str) -> list[Markup]:
    plan = _plan(old_line, new_line)
    if plan is not None:
        return [_line(*_inline_pieces(*plan))]
    return [_line(*_marked(old_line, "rm"), kind="gone"), _line(*_marked(new_line, "ins"), kind="added")]


def redline(before: str, after: str) -> Markup | None:
    """Return the changed text marked up, or None when it is too large to compare quickly."""
    if len(before) > MAX_DOCUMENT_CHARS or len(after) > MAX_DOCUMENT_CHARS:
        return None
    old, new = _split(before, after)
    if len(old) > MAX_DOCUMENT_LINES or len(new) > MAX_DOCUMENT_LINES:
        return None
    show_everything = len(before) <= MAX_REDLINE_CHARS and len(after) <= MAX_REDLINE_CHARS
    ops = difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes()
    rows: list[Markup] = []
    last = len(ops) - 1
    for index, (tag, i1, i2, j1, j2) in enumerate(ops):
        if tag == "equal":
            lines = old[i1:i2]
            keep_head = CONTEXT_LINES if index > 0 else 0
            keep_tail = CONTEXT_LINES if index < last else 0
            if show_everything or len(ops) == 1 or len(lines) <= keep_head + keep_tail + 1:
                rows += [_line(block(text)) for text in lines]
                continue
            rows += [_line(block(text)) for text in lines[:keep_head]]
            rows.append(_fold(len(lines) - keep_head - keep_tail))
            rows += [_line(block(text)) for text in lines[len(lines) - keep_tail:]]
        elif tag == "replace" and (i2 - i1) == (j2 - j1):
            for text_old, text_new in zip(old[i1:i2], new[j1:j2], strict=True):
                rows += _pair(text_old, text_new)
        else:
            rows += [_line(*_marked(text, "rm"), kind="gone") for text in old[i1:i2]]
            rows += [_line(*_marked(text, "ins"), kind="added") for text in new[j1:j2]]
    return el("div", *rows, class_="redline lines")


# -- a one-glance summary of the first change, for list rows ----------------------------------------------------

def _clip(text: str, limit: int, *, keep_end: bool) -> str:
    """Shorten to roughly `limit` characters at a word boundary, marking the cut with an ellipsis."""
    if len(text) <= limit:
        return text
    if keep_end:
        cut = text[len(text) - limit:]
        space = cut.find(" ")
        return "\u2026" + (cut[space + 1:] if space != -1 else cut)
    cut = text[:limit]
    space = cut.rfind(" ")
    return (cut[:space] if space != -1 else cut) + "\u2026"


def _words_snippet(plan: tuple[list[tuple], list[str], list[str]], context: int) -> tuple[Markup, int]:
    ops, old_tokens, new_tokens = plan
    changes = [i for i, op in enumerate(ops) if op[0] != "equal"]
    index = changes[0]
    _, i1, i2, j1, j2 = ops[index]
    left = _clip("".join(old_tokens[:i1]), context, keep_end=True)
    following = ops[index + 1] if index + 1 < len(ops) else None
    right_tokens = old_tokens[following[1]:following[2]] if following is not None and following[0] == "equal" else []
    right = _clip("".join(right_tokens), context, keep_end=False)
    removed = _clip("".join(old_tokens[i1:i2]), 300, keep_end=False)
    added = _clip("".join(new_tokens[j1:j2]), 300, keep_end=False)
    pieces: list[Markup] = [block(left)] if left else []
    pieces += _marked(removed, "rm") + _marked(added, "ins")
    if right:
        pieces.append(block(right))
    return el("span", *pieces), len(changes) - 1


def change_snippet(before: str, after: str, context: int = 70) -> tuple[Markup, str | None] | None:
    """The first change in a text, for a list row, and a short note about the rest ("3 lines changed").

    This answers "what happened" at a glance: a long file that gained one sentence shows that sentence, not
    the start of the file. A rewritten line shows what it says now. Line breaks never reach the result; it is
    escaped like everything else.
    """
    if len(before) > MAX_DOCUMENT_CHARS or len(after) > MAX_DOCUMENT_CHARS:
        return None
    old, new = _split(before, after)
    if len(old) > MAX_DOCUMENT_LINES or len(new) > MAX_DOCUMENT_LINES:
        return None
    changed = [op for op in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes() if op[0] != "equal"]
    if not changed:
        return None
    lines_changed = sum(max(i2 - i1, j2 - j1) for _, i1, i2, j1, j2 in changed)
    _, i1, i2, j1, j2 = changed[0]
    old_line = next((text for text in old[i1:i2] if text.strip()), None)
    new_line = next((text for text in new[j1:j2] if text.strip()), None)

    note: str | None = None
    if old_line is not None and new_line is not None:
        plan = _plan(old_line, new_line)
        if plan is not None:
            snippet, more = _words_snippet(plan, context)
            if more and lines_changed == 1:
                note = f"and {more} more change{'' if more == 1 else 's'}"
        else:  # a rewrite: show what it says now (and the old text too when that is short)
            pieces: list[Markup] = []
            if len(old_line) <= 80:
                pieces += _marked(old_line.strip(), "rm") + [block(" ")]
            pieces += _marked(_clip(new_line.strip(), 220, keep_end=False), "ins")
            snippet = el("span", *pieces)
            if len(old_line) > 80 and lines_changed == 1:
                note = "rewritten"
    elif new_line is not None:
        snippet = el("span", *_marked(_clip(new_line.strip(), 220, keep_end=False), "ins"))
    elif old_line is not None:
        snippet = el("span", *_marked(_clip(old_line.strip(), 220, keep_end=False), "rm"))
    else:
        return None
    if lines_changed >= 2:
        note = f"{lines_changed} lines changed"
    return snippet, note
