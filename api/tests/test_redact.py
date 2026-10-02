import pytest

from api.app.redact import redact


@pytest.mark.parametrize(
    "text, expected",
    [
        ("email me at jane.doe+dsu@students.desu.edu please", "email me at [email] please"),
        ("JDOE@GMAIL.COM", "[email]"),
        ("call 302-857-6060", "call [phone]"),
        ("call (302) 857-6060", "call [phone]"),
        ("call 302.857.6060", "call [phone]"),
        ("call +1 302 857 6060", "call [phone]"),
        ("call 3028576060", "call [phone]"),
        ("call 857-6060", "call [phone]"),
        ("my SSN is 123-45-6789", "my SSN is [ssn]"),
        ("my SSN is 123 45 6789", "my SSN is [ssn]"),
        ("my SSN is 123456789", "my SSN is [number]"),
        ("student ID 900123456", "student ID [number]"),
        ("ID D00123456", "ID D[number]"),
        ("account 12345678901234", "account [number]"),
        ("card 4111 1111 1111 1111", "card [number]"),
        ("card 4111-1111-1111-1111", "card [number]"),
    ],
)
def test_personal_details_are_redacted(text, expected):
    assert redact(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "When is fall 2026 move-in?",
        "Fees for the 2026-2027 academic year",
        "1200 N. DuPont Highway, Dover, DE 19901",
        "The 19 Meal plan costs $2,833.50 per semester.",
        "Is MATH 101 offered in room 204?",
    ],
)
def test_ordinary_numbers_are_kept(text):
    assert redact(text) == text


def test_several_details_in_one_question():
    text = "I'm jdoe@desu.edu, 302-555-0199, ID 900123456. Is my fee paid?"
    assert redact(text) == "I'm [email], [phone], ID [number]. Is my fee paid?"
