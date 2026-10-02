"""Remove personal details from text before it is stored. Anonymous visitors still type their
email, phone number, or student ID into a question, so nothing a visitor typed is stored or logged
before going through redact()."""

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
