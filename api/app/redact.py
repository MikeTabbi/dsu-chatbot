"""Remove personal details from text before it is stored. Anonymous visitors still type their
email, phone number, or student ID into a question, so nothing a visitor typed is stored or logged
before going through redact(). Answers go through redact_echoes(), which removes only what the
student typed and keeps DSU's own phone numbers and emails."""

import re

# Checked in this order, so an SSN or a card number isn't half-matched as a phone number first.
# (?<!\d) and (?!\d) instead of \b: "D00123456" still has its digits removed.
PATTERNS = [
    ("[email]", re.compile(r"[\w.%+-]+@[\w-]+(?:\.[\w-]+)+")),
    ("[ssn]", re.compile(r"(?<!\d)\d{3}([- ])\d{2}\1\d{4}(?!\d)")),
    ("[number]", re.compile(r"(?<!\d)\d{4}(?:[ -]\d{4}){2,}(?!\d)")),  # card numbers in groups
    (
        "[phone]",
        re.compile(
            r"(?<![\d+])(?:\+?1[\s.-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.-]?)\d{3}[\s.-]?\d{4}(?!\d)"
            r"|(?<!\d)\d{3}[\s.-]\d{4}(?!\d)"  # 857-6060, without an area code
        ),
    ),
    # Student IDs, account numbers, SSNs typed without dashes. Years and ZIP codes are shorter.
    ("[number]", re.compile(r"(?<!\d)\d{6,}(?!\d)")),
]


def redact(text: str) -> str:
    """text with emails, SSNs, phone numbers, and long digit strings replaced by placeholders
    like [email] and [phone]."""
    for placeholder, pattern in PATTERNS:
        text = pattern.sub(placeholder, text)
    return text


def redact_echoes(answer: str, question: str) -> str:
    """answer with only the personal details the student typed in question replaced, like
    redact() would. Everything else in the answer comes from DSU's own pages (the main phone line,
    an office's email), so it's kept for whoever reviews the answer.

    A detail counts as the student's when it matches one in the question, compared by digits for
    numbers (Claude may write (302) 555-0199 as 302-555-0199 or +1 302 555 0199) and ignoring
    case for emails. A DSU number the student typed into the question is removed too: it's what
    they typed, and the question is redacted the same way.
    """
    typed = {_key(placeholder, value) for _, _, placeholder, value in _matches(question)}
    if not typed:
        return answer
    parts, last = [], 0
    for start, end, placeholder, value in _matches(answer):
        if _echoes(_key(placeholder, value), typed):
            parts += [answer[last:start], placeholder]
            last = end
    parts.append(answer[last:])
    return "".join(parts)


def _matches(text: str) -> list[tuple[int, int, str, str]]:
    """(start, end, placeholder, value) for each detail in text, in text order. PATTERNS are tried
    in their order and a later one can't match inside an earlier one's match, as in redact()."""
    found: list[tuple[int, int, str, str]] = []
    for placeholder, pattern in PATTERNS:
        for m in pattern.finditer(text):
            if not any(m.start() < end and start < m.end() for start, end, _, _ in found):
                found.append((m.start(), m.end(), placeholder, m.group()))
    return sorted(found)


def _key(placeholder: str, value: str) -> str:
    """What two ways of writing the same detail have in common: an email in lowercase, otherwise
    the digits (a phone number without its leading country code 1)."""
    if placeholder == "[email]":
        return value.lower()
    digits = re.sub(r"\D", "", value)
    if placeholder == "[phone]" and len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def _echoes(key: str, typed: set[str]) -> bool:
    """True when key is one of the typed details, or the same phone number with or without its
    area code (555-0199 and 302-555-0199)."""
    if key in typed:
        return True
    return any(
        (len(key) == 7 and len(t) == 10 and t.endswith(key))
        or (len(key) == 10 and len(t) == 7 and key.endswith(t))
        for t in typed
        if t.isdigit() and key.isdigit()
    )
