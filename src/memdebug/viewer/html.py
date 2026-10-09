"""Safe-by-construction HTML.

Memory text is untrusted: an attacker can plant `<script>` or markup in a memory. Instead of
remembering to escape at every use, pages are built only through `el()`:

  * a plain string child is ALWAYS escaped (and control, bidi and line-separator characters are
    made visible), so forgetting to escape is not possible;
  * only `Markup` (created by this module) is inserted as-is;
  * tags and attributes come from fixed allow-lists: no script, style, iframe, image, object or
    event-handler attributes can be produced at all;
  * `href` and `action` accept only internal paths that start with a single `/`.

The pages carry no JavaScript and the server adds a Content-Security-Policy that forbids it, so even
a bug here could not run code.
"""
from __future__ import annotations

import html as _html
import re
import unicodedata
from typing import Iterable

from ..textsafe import _BIDI, _UNSAFE_CATEGORIES, safe_text

__all__ = ["Markup", "el", "inline", "block", "raw_doctype"]


class Markup(str):
    """HTML that is already safe. Only functions in this module create it."""

    __slots__ = ()


_TAGS = frozenset({
    "html", "head", "meta", "title", "link", "body", "header", "nav", "main", "aside", "section", "footer",
    "h1", "h2", "h3", "p", "div", "span", "a", "table", "thead", "tbody", "tr", "th", "td", "caption",
    "ul", "ol", "li", "pre", "code", "details", "summary", "form", "label", "select", "option", "button",
    "input", "strong", "em", "br", "small", "time", "dl", "dt", "dd",
})
_VOID = frozenset({"meta", "link", "br", "input"})
_ATTRS = frozenset({
    "class", "href", "id", "lang", "charset", "name", "content", "rel", "type", "for", "value", "selected",
    "checked", "scope", "colspan", "aria-label", "aria-current", "method", "action", "datetime", "title",
    "role", "open", "disabled", "http-equiv", "media",
})
_URL_ATTRS = frozenset({"href", "action"})
_INTERNAL_URL = re.compile(r"^/(?!/)[A-Za-z0-9_\-./?=&%:~+,]*\Z")


def raw_doctype() -> Markup:
    return Markup("<!doctype html>")


def _attr_name(name: str) -> str:
    return name.rstrip("_").replace("_", "-")


def _render_attrs(attrs: dict) -> str:
    parts: list[str] = []
    for key, value in attrs.items():
        name = _attr_name(key)
        if name not in _ATTRS:
            raise ValueError(f"attribute not allowed: {name!r}")
        if value is None or value is False:
            continue
        if value is True:
            parts.append(name)
            continue
        text = str(value)
        if name in _URL_ATTRS and not _INTERNAL_URL.match(text):
            raise ValueError(f"only internal paths are allowed in {name}: {text!r}")
        parts.append(f'{name}="{_html.escape(safe_text(text, None), quote=True)}"')
    return (" " + " ".join(parts)) if parts else ""


def _render_children(children: Iterable) -> str:
    out: list[str] = []
    for child in children:
        if child is None or child is False:
            continue
        if isinstance(child, Markup):
            out.append(str(child))
        elif isinstance(child, (list, tuple)):
            out.append(_render_children(child))
        elif isinstance(child, (int, float)) and not isinstance(child, bool):
            out.append(_html.escape(str(child)))
        else:  # any ordinary string is untrusted by default
            out.append(_html.escape(safe_text(child, None), quote=True))
    return "".join(out)


def el(tag: str, *children, **attrs) -> Markup:
    if tag not in _TAGS:
        raise ValueError(f"tag not allowed: {tag!r}")
    opening = f"<{tag}{_render_attrs(attrs)}>"
    if tag in _VOID:
        if children:
            raise ValueError(f"<{tag}> cannot have children")
        return Markup(opening)
    return Markup(f"{opening}{_render_children(children)}</{tag}>")


def inline(text: object, limit: int | None = 200) -> Markup:
    """Untrusted text on one line, escaped and shortened."""
    return Markup(_html.escape(safe_text(text, limit), quote=True))


def block(text: object, limit: int = 20_000) -> Markup:
    """Untrusted multi-line text for a <pre>: line breaks and tabs are kept, every other control,
    format, bidi or line-separator character is shown as a visible escape.
    """
    value = str(text if text is not None else "")
    total = len(value)
    shown = value[:limit]
    pieces: list[str] = []
    skip_next = False
    for index, ch in enumerate(shown):
        if skip_next:
            skip_next = False
            continue
        if ch == "\r":
            if shown[index + 1:index + 2] == "\n":
                skip_next = True
                pieces.append("\n")
                continue
            pieces.append("\\r")
        elif ch in ("\n", "\t"):
            pieces.append(ch)
        elif ch in _BIDI or unicodedata.category(ch) in _UNSAFE_CATEGORIES:
            code = ord(ch)
            pieces.append(f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}" if code < 0x10000 else f"\\U{code:08x}")
        else:
            pieces.append(ch)
    out = _html.escape("".join(pieces), quote=True)
    if total > limit:
        out += _html.escape(f"\n... (shortened: showing the first {limit} of {total} characters)")
    return Markup(out)
