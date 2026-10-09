"""Helpers for handling text that came from a memory store.

Memory text is untrusted. It may be planted by an attacker, for example through an email an
agent read. Two rules follow:
  * bound its size before storing it (bound_text), and
  * never print it raw (safe_text), so it cannot move the cursor, recolour the terminal,
    forge extra timeline lines or reorder what the reader sees.
"""
from __future__ import annotations

import hashlib
import unicodedata

MAX_TEXT_CHARS = 100_000

_NAMED = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}
_BIDI = set("\u200e\u200f\u061c\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
_UNSAFE_CATEGORIES = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}


def bound_text(text: str, max_chars: int = MAX_TEXT_CHARS) -> str:
    """Cap the length of a text.

    A cut text carries a hash of the full text, so two different long texts that share a prefix still compare as different.
    A text of `max_chars` characters or fewer is returned unchanged.
    """
    if len(text) <= max_chars:
        return text
    digest = hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:16]
    return f"{text[:max_chars]}...[cut: {len(text)} chars, sha256 {digest}]"


def _escape(ch: str) -> str:
    code = ord(ch)
    if code < 0x100:
        return f"\\x{code:02x}"
    if code < 0x10000:
        return f"\\u{code:04x}"
    return f"\\U{code:08x}"


def safe_text(value: object, limit: int | None = 200) -> str:
    """Single-line, printable rendering of untrusted text.

    Control, format, bidi and line-separator characters (and other unassigned, private-use or surrogate characters) are shown as
    visible escapes. Newlines, carriage returns and tabs become the two-character escapes. Any value is accepted and converted
    with `str`.

    Args:
        value: The text, or any object to show as text.
        limit: The most characters to return before cutting off with "..."; None for no limit.
    """
    pieces: list[str] = []
    length = 0
    for ch in str(value):
        if ch in _NAMED:
            piece = _NAMED[ch]
        elif ch in _BIDI or unicodedata.category(ch) in _UNSAFE_CATEGORIES:
            piece = _escape(ch)
        else:
            piece = ch
        if limit is not None and length + len(piece) > limit:
            pieces.append("...")
            break
        pieces.append(piece)
        length += len(piece)
    return "".join(pieces)


def has_unsafe_chars(text: str) -> bool:
    """True if the text contains anything safe_text would have to escape."""
    return any(ch in _BIDI or unicodedata.category(ch) in _UNSAFE_CATEGORIES for ch in text)


def console_safe(text: str, encoding: str | None = None) -> str:
    """Make text printable on a console whose encoding cannot show every character.

    An example is a Windows code page. Unencodable characters become visible escapes instead of raising.

    Args:
        text: The text to print.
        encoding: The console's encoding. Defaults to that of `sys.stdout`, then UTF-8.
    """
    import sys

    target = encoding or getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        return text.encode(target, "backslashreplace").decode(target)
    except LookupError:
        return text.encode("ascii", "backslashreplace").decode("ascii")
