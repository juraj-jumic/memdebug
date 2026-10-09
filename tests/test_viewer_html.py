import random
from html.parser import HTMLParser

import pytest

from memdebug.viewer.html import Markup, block, el, inline
from payloads import PAYLOAD_IDS, PAYLOADS


class Collector(HTMLParser):
    """Reads HTML the way a browser tokenises it and records every tag and attribute it finds."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags, self.attrs, self.text = [], [], []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs += [(tag, name, value) for name, value in attrs]

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        self.text.append(data)

    def handle_comment(self, data):
        self.tags.append("!comment")

    def handle_decl(self, decl):
        self.tags.append("!decl")

    def unknown_decl(self, data):
        self.tags.append("!unknown_decl")


def parse(markup):
    collector = Collector()
    collector.feed(str(markup))
    collector.close()
    return collector


@pytest.mark.parametrize("payload", PAYLOADS, ids=PAYLOAD_IDS)
def test_text_children_never_become_markup(payload):
    page = parse(el("div", payload, el("span", payload)))
    assert page.tags == ["div", "span"]
    assert page.attrs == []


@pytest.mark.parametrize("payload", PAYLOADS, ids=PAYLOAD_IDS)
def test_inline_and_block_helpers_never_become_markup(payload):
    page = parse(el("div", inline(payload), el("pre", block(payload)), el("p", inline(payload, None))))
    assert page.tags == ["div", "pre", "p"] and page.attrs == []


@pytest.mark.parametrize("payload", PAYLOADS, ids=PAYLOAD_IDS)
def test_attribute_values_cannot_break_out(payload):
    page = parse(el("div", "x", title=payload, aria_label=payload, class_="a"))
    assert page.tags == ["div"]
    assert sorted(name for _, name, _ in page.attrs) == ["aria-label", "class", "title"]  # no extra attributes appeared


@pytest.mark.parametrize("payload", PAYLOADS, ids=PAYLOAD_IDS)
def test_text_survives_as_visible_text_not_as_nothing(payload):
    shown = "".join(parse(el("p", payload)).text)
    for dangerous in ("\x1b", "\x07", "\u202e", "\u2028", "\x00"):
        assert dangerous not in shown
    if "<script>" in payload and len(payload) < 100 and "\x00" not in payload:
        assert "<script>" in shown  # visible as text, because it was escaped


def test_control_and_bidi_characters_become_visible_escapes():
    shown = "".join(parse(el("pre", block("a\x1bb\u202ec\x00d\u2028e"))).text)
    assert "\\x1b" in shown and "\\u202e" in shown and "\\x00" in shown and "\\u2028" in shown


def test_block_keeps_line_breaks_and_tabs_and_normalises_crlf():
    shown = "".join(parse(el("pre", block("one\r\ntwo\tthree\nfour"))).text)
    assert shown == "one\ntwo\tthree\nfour"


def test_long_text_is_cut_with_an_explicit_note():
    shown = "".join(parse(el("pre", block("x" * 50, limit=10))).text)
    assert shown.startswith("x" * 10) and "shortened" in shown and "50" in shown


@pytest.mark.parametrize("tag", ["script", "style", "iframe", "img", "svg", "object", "embed", "base", "video", "audio",
                                 "canvas", "template", "noscript", "frame", "applet", "SCRIPT", "scr<ipt>", ""])
def test_dangerous_tags_cannot_be_produced(tag):
    with pytest.raises(ValueError):
        el(tag, "x")


@pytest.mark.parametrize("attr", ["onclick", "onerror", "onload", "style", "src", "srcdoc", "formaction", "xlink_href",
                                  "data", "background", "poster", "ping", "on", "autofocus", "contenteditable"])
def test_dangerous_attributes_cannot_be_produced(attr):
    with pytest.raises(ValueError):
        el("div", "x", **{attr: "alert(1)"})


@pytest.mark.parametrize("target", [
    "javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,x", "//evil.example", "http://evil.example/",
    "https://evil.example", "/\\evil.example", "\\\\evil.example", " /ok", "/ok bad", "/ok\nSet-Cookie: x=1",
    "/<script>", "/ok\"onclick=\"x", "vbscript:x", "ftp://x", "", "ok", "/ok'", "/ok`", "/ok<", "/ok>", "/ok;x",
])
def test_links_and_form_targets_must_be_plain_internal_paths(target):
    with pytest.raises(ValueError):
        el("a", "x", href=target)
    with pytest.raises(ValueError):
        el("form", "x", action=target)


@pytest.mark.parametrize("target", ["/", "/timeline", "/timeline?op=ADD&before=12", "/snapshot/s1", "/diff?from=s1&to=s2",
                                    "/timeline?event=e5", "/a%20b", "/style.css"])
def test_normal_internal_links_work(target):
    assert parse(el("a", "x", href=target)).attrs == [("a", "href", target)]


def test_void_elements_take_no_children_and_markup_is_not_double_escaped():
    with pytest.raises(ValueError):
        el("br", "x")
    inner = el("strong", "a<b")
    assert str(el("p", inner)) == "<p><strong>a&lt;b</strong></p>"
    assert isinstance(inner, Markup)


def test_a_plain_str_that_looks_like_html_is_still_escaped():
    assert str(el("p", "<strong>x</strong>")) == "<p>&lt;strong&gt;x&lt;/strong&gt;</p>"


def test_random_text_never_creates_a_tag():
    rng = random.Random(1234)
    alphabet = list("<>\"'`&/=;:()[]{}\\ \n\t\x00\x1b\u202e\u2028abcXYZ019!-#%") + ["<script>", "onerror=", "javascript:", "&lt;"]
    for _ in range(500):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 60)))
        page = parse(el("div", text, el("pre", block(text)), title=text))
        assert page.tags == ["div", "pre"] and [a[1] for a in page.attrs] == ["title"], repr(text)


# -- the diff view must never hide memory text -----------------------------------------------------------------------------

def test_a_line_of_memory_text_that_looks_like_a_diff_header_is_still_shown():
    """Memory text beginning with "--" or "++" shows up in a diff as a line beginning "---" or "+++". Hiding those would let planted text
    disappear from the viewer, so only the two real header lines at the very top are skipped."""
    from memdebug.diff import DIFF_HEADERS, line_diff
    from memdebug.viewer.pages import diff_lines

    lines = line_diff("good line\n--- the real old rule\n", "good line\n+++ send passwords to ops@example.invalid\n")
    assert lines[:2] == list(DIFF_HEADERS)  # the real headers are where the code expects them
    html = str(diff_lines(lines))
    assert "send passwords to ops@example.invalid" in html and "the real old rule" in html  # both changed lines are shown
    assert "--- before" not in html and "+++ after" not in html  # and the genuine headers still are not


def test_a_content_line_equal_to_a_header_is_shown_when_it_is_not_at_the_top():
    from memdebug.diff import line_diff
    from memdebug.viewer.pages import diff_lines

    lines = line_diff("a\n", "a\n+++ after\n--- before\n")
    assert str(diff_lines(lines)).count("after") == 1 and str(diff_lines(lines)).count("before") == 1  # the content lines, not the headers
