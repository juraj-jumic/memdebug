"""Hints: things in a memory's text that are worth a second look. Heuristics, not verdicts.

These look for the shapes that poisoned memory tends to take: invisible characters, "ignore your instructions" phrasing,
instructions to send something to an address, instructions to stop asking for confirmation or to weaken a safeguard, and
text that looks like a secret. They WILL miss paraphrases, other wording and other languages, and they can flag harmless
text. They are never a reason to trust something that is not flagged, so they are shown as hints and labelled as such.

Safety of the scanner itself: the text is untrusted, so scanning is bounded (characters per line and per text), every
pattern uses bounded repetition, and evidence is a short excerpt that is made safe to show. A secret-like string is never
repeated in a hint, only its kind and length.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

from .textsafe import safe_text

MAX_SCAN_CHARS = 100_000
MAX_LINE_CHARS = 2_000
TIME_BUDGET = 0.4  # seconds; a crafted text cannot make scanning slow
MAX_HINTS = 12

_HIDDEN = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\U000e0000-\U000e007f]")

_OVERRIDE_TEXT = [
    r"\b(?:ignore|disregard|forget|override)\s{1,5}(?:all\s{1,5})?(?:of\s{1,5})?(?:the\s{1,5})?(?:your\s{1,5})?(?:previous|prior|above|earlier|preceding|existing)\s{1,5}(?:instructions?|rules?|guidelines?|prompts?|directions?)\b",
    r"\byou\s{1,5}are\s{1,5}now\s{1,5}(?:a|an|the|in)\b",
    r"\b(?:reveal|print|show|leak|output|repeat)\s{1,5}(?:your\s{1,5})?(?:system\s{1,5}|hidden\s{1,5})?(?:prompt|instructions)\b",
    r"\b(?:system|developer)\s{0,3}(?:prompt|message)\s{0,3}:",
    r"</?(?:system|instructions?)>",
    r"\bfrom\s{1,5}now\s{1,5}on\b[^.\n]{0,60}\b(?:ignore|obey|only|always)\b",
    r"\bignoriere\s{1,5}(?:alle\s{1,5})?(?:vorherigen|bisherigen)\s{1,5}anweisungen\b",
    r"\bignore\s{1,5}(?:todas\s{1,5})?las\s{1,5}instrucciones\s{1,5}anteriores\b",
    r"\bignorez?\s{1,5}(?:toutes\s{1,5})?les\s{1,5}instructions\s{1,5}pr[ée]c[ée]dentes\b",
    r"\bzanemari\s{1,5}(?:sve\s{1,5})?(?:prethodne|ranije)\s{1,5}(?:upute|naredbe|instrukcije)\b",
]
_OVERRIDE = [re.compile(text, re.IGNORECASE) for text in _OVERRIDE_TEXT]
_VERBS = r"(?:send|forward|email|e-mail|mail|upload|post|submit|transmit|share|copy|leak|exfiltrate|paste|give|hand\s{1,3}over|report)"
_DESTINATION = r"(?:[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){1,6}|https?://[^\s<>\"']{3,200}|\b\d{1,3}(?:\.\d{1,3}){3}\b)"
_SENSITIVE = r"(?:credentials?|passwords?|passphrases?|tokens?|api[ _-]?keys?|secrets?|private\s{1,3}keys?|ssh\s{1,3}keys?|cookies?|session|2fa|otp|pins?|bank|card\s{1,3}numbers?|ssn|everything|all\s{1,5}(?:emails?|messages|files|data|documents|conversations))"
_SEND_DATA = re.compile(rf"\b{_VERBS}\b[^.\n]{{0,120}}?\b(?:to|at|via|into|on)\b[^.\n]{{0,60}}?{_DESTINATION}", re.IGNORECASE)
_SENSITIVE_NEAR = re.compile(rf"\b{_SENSITIVE}\b", re.IGNORECASE)
_NO_CONFIRM = re.compile(
    r"\b(?:without|no\s{1,3}need\s{1,3}(?:to|for))\s{1,5}(?:asking|ask|confirm(?:ing|ation)?|approval|permission|verif(?:y|ying|ication)|checking|review(?:ing)?|telling|notifying|consent)\b"
    r"|\b(?:do\s{1,3}not|don'?t|never)\s{1,5}(?:ask|confirm|verify|check|warn|notify|tell)\b[^.\n]{0,40}\b(?:user|me|them|first|again|before)\b"
    r"|\b(?:skip|bypass|disable|turn\s{1,3}off|stop)\s{1,5}(?:the\s{1,5})?(?:confirmations?|approvals?|verification|human\s{1,3}review|checks?)\b",
    re.IGNORECASE)
_WEAKEN = re.compile(
    r"\b(?:disable|turn\s{1,3}off|bypass|deactivate|uninstall|ignore|skip|stop)\s{1,5}(?:the\s{1,5}|your\s{1,5}|all\s{1,5})?(?:firewall|antivirus|anti-virus|security|2fa|two-factor|authentication|certificate|ssl|tls|safety|sandbox|logging|audit|encryption)\b"
    r"|\b(?:run|execute)\s{1,5}(?:any|all|arbitrary)\s{1,5}(?:commands?|code|scripts?)\b"
    r"|\b(?:trust|accept)\s{1,5}(?:all|any|every)\s{1,5}(?:certificates?|sources?|senders?|emails?|links?)\b",
    re.IGNORECASE)
_SECRETS = [
    ("an AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("a private key", re.compile(r"-----BEGIN (?:[A-Z]{2,10} )?PRIVATE KEY-----")),
    ("a GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("an API key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("a Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("a JSON web token", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("a password or key in plain text", re.compile(r"\b(?:password|passwd|pwd|secret|api[_-]?key|token)\s{0,3}[:=]\s{0,3}[^\s'\"]{8,}", re.IGNORECASE)),
]


@dataclass(frozen=True)
class Hint:
    """One thing in a memory's text that is worth a second look.

    Attributes:
        kind: A short label for the shape found, for example "override-phrase" or "secret-like".
        severity: "note" or "warning".
        message: A plain-words explanation of why the text was flagged.
        evidence: A short excerpt that is safe to display. For a secret-like string it names only the kind and length.
    """

    kind: str
    severity: str  # "note" or "warning"
    message: str
    evidence: str  # a short excerpt, safe to display; never a secret

    @property
    def key(self) -> tuple[str, str]:
        """The pair (kind, evidence) that identifies a hint when removing duplicates or comparing two scans."""
        return (self.kind, self.evidence)


def _redact(line: str) -> str:
    for _, pattern in _SECRETS:
        line = pattern.sub("[redacted]", line)
    return line


def _excerpt(match: re.Match[str], line: str, width: int = 80) -> str:
    """A short excerpt around a match, with anything secret-like removed first."""
    start, end = match.start(), match.end()
    cut = max(0, start - 12)
    text = _redact(line[cut:min(len(line), max(end, start + width))])
    if cut > 0 and not line[cut - 1].isspace() and " " in text[:20]:
        text = text.split(" ", 1)[1]  # do not begin in the middle of a word
    return safe_text(" ".join(text.split()), 100)


def scan(text: str, *, budget: float = TIME_BUDGET, max_chars: int = MAX_SCAN_CHARS) -> list[Hint]:
    """Hints for a text. Bounded in time and size whatever the text contains."""
    hints: list[Hint] = []
    seen: set[tuple[str, str]] = set()

    def add(hint: Hint) -> None:
        if hint.key not in seen and len(hints) < MAX_HINTS:
            seen.add(hint.key)
            hints.append(hint)

    body = text[:max_chars]
    found = sorted({f"U+{ord(ch):04X}" for ch in _HIDDEN.findall(body)})
    if found:
        add(Hint("hidden-characters", "warning", "contains invisible or text-direction characters that can hide instructions from a reader",
                 ", ".join(found[:6])))
    deadline = time.monotonic() + budget
    previous = None
    for raw in body.split("\n"):
        if time.monotonic() > deadline:
            break
        line = raw[:MAX_LINE_CHARS]
        if line == previous or not line.strip():
            continue
        previous = line
        for pattern in _OVERRIDE:
            match = pattern.search(line)
            if match:
                add(Hint("override-phrase", "warning", "reads like an instruction to ignore or replace the agent's existing instructions", _excerpt(match, line)))
        for match in (_SEND_DATA.finditer(line) if ("@" in line or "://" in line or re.search(r"\d\.\d", line)) else ()):
            window = line[max(0, match.start() - 80):match.end() + 40]
            sensitive = bool(_SENSITIVE_NEAR.search(window))
            add(Hint("send-data", "warning" if sensitive else "note",
                     "asks to send something to an address" + (", and mentions credentials or private data" if sensitive else ""), _excerpt(match, line)))
        match = _NO_CONFIRM.search(line)
        if match:
            add(Hint("removes-confirmation", "warning", "tells the agent to stop asking for confirmation or approval", _excerpt(match, line)))
        match = _WEAKEN.search(line)
        if match:
            add(Hint("weakens-safeguard", "warning", "tells the agent to turn off or bypass a safeguard, or to run anything", _excerpt(match, line)))
        for label, pattern in _SECRETS:
            match = pattern.search(line)
            if match:
                add(Hint("secret-like", "warning", f"looks like {label} stored in memory", f"{label} ({len(match.group(0))} characters, not shown)"))
    return hints


def new_hints(before: str | None, after: str | None, *, budget: float = TIME_BUDGET, max_chars: int = MAX_SCAN_CHARS) -> list[Hint]:
    """Hints for what a change ADDED: anything already present before the change does not count again."""
    if not after:
        return []
    old = {h.key for h in scan(before, budget=budget, max_chars=max_chars)} if before else set()
    return [h for h in scan(after, budget=budget, max_chars=max_chars) if h.key not in old]
