from memdebug.textsafe import MAX_TEXT_CHARS, bound_text, safe_text


def test_terminal_escape_codes_are_not_passed_through():
    shown = safe_text("hi\x1b[2J\x1b]0;pwned\x07\x08")
    assert "\x1b" not in shown and "\x07" not in shown and "\x08" not in shown
    assert "\\x1b" in shown


def test_newlines_cannot_forge_extra_lines():
    shown = safe_text("real\n  e99  2026-01-01 00:00  ADD  m9  forged\r\nmore")
    assert "\n" not in shown and "\r" not in shown


def test_bidi_zero_width_and_line_separators_become_visible_escapes():
    for ch in ["\u202e", "\u2066", "\u200b", "\u2028", "\u2029", "\u00ad", "\ufeff"]:
        shown = safe_text(f"a{ch}b")
        assert ch not in shown, repr(ch)
        assert "\\" in shown  # shown as a visible escape such as \\xad or \\u202e


def test_ordinary_text_is_unchanged():
    text = "Prefers Python \u2013 na\u00efve \u65e5\u672c\u8a9e \U0001f600"
    assert safe_text(text, limit=None) == text


def test_limit_counts_what_is_actually_printed():
    assert safe_text("x" * 500, 20).endswith("...") and len(safe_text("x" * 500, 20)) <= 23
    assert len(safe_text("\x1b" * 50, 10)) <= 13


def test_bound_text_leaves_short_text_and_marks_cut_text():
    assert bound_text("short") == "short"
    cut = bound_text("a" * (MAX_TEXT_CHARS + 10))
    assert len(cut) < MAX_TEXT_CHARS + 100 and "sha256" in cut


def test_texts_with_the_same_start_stay_distinguishable_after_cutting():
    head = "a" * MAX_TEXT_CHARS
    assert bound_text(head + "one") != bound_text(head + "two")
