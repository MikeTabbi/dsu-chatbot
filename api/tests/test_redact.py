import pytest

from api.app.redact import redact, redact_echoes


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


# Answers: only what the student typed is removed (redact_echoes).


@pytest.mark.parametrize(
    "answer",
    [
        "Call the main line at 302.857.6060 during the day.",
        "Email admissions@desu.edu or call (302) 857-6000.",
        "Student Accounts is at 302-857-6200, extension 4.",
        "The 19 Meal plan costs $2,833.50 per semester.",
    ],
)
def test_dsu_details_in_the_answer_are_kept(answer):
    question = "I'm jdoe@desu.edu, 302-555-0199, ID 900123456. Who do I call about my bill?"
    assert redact_echoes(answer, question) == answer


@pytest.mark.parametrize(
    "question, answer, expected",
    [
        # The same phone number, however Claude writes it
        ("text me at (302) 555-0199", "I can't text 302-555-0199.", "I can't text [phone]."),
        ("text me at 302-555-0199", "I can't text (302) 555-0199.", "I can't text [phone]."),
        ("text me at 302-555-0199", "I can't text +1 302 555 0199.", "I can't text [phone]."),
        ("text me at 3025550199", "I can't text 302.555.0199.", "I can't text [phone]."),
        ("text me at 302-555-0199", "I can't text 555-0199.", "I can't text [phone]."),
        ("text me at 555-0199", "I can't text 302-555-0199.", "I can't text [phone]."),
        # Emails, ignoring case
        ("I'm JDoe@Students.DESU.edu", "Email jdoe@students.desu.edu.", "Email [email]."),
        # Student IDs and other numbers
        ("my ID is 900123456", "I can't look up 900123456.", "I can't look up [number]."),
        ("my ID is D00123456", "I can't look up D00123456.", "I can't look up D[number]."),
        ("SSN 123456789", "Never share 123-45-6789 here.", "Never share [ssn] here."),
        ("card 4111 1111 1111 1111", "Don't type 4111-1111-1111-1111.", "Don't type [number]."),
    ],
)
def test_the_students_own_details_are_removed_from_the_answer(question, answer, expected):
    assert redact_echoes(answer, question) == expected


def test_only_the_students_details_go_when_both_are_in_the_answer():
    question = "I'm jdoe@desu.edu and my number is 302-555-0199. Who handles housing?"
    answer = (
        "Housing is at 302-857-6090 and housing@desu.edu. "
        "I can't contact you at jdoe@desu.edu or 302.555.0199."
    )
    assert redact_echoes(answer, question) == (
        "Housing is at 302-857-6090 and housing@desu.edu. "
        "I can't contact you at [email] or [phone]."
    )


def test_a_dsu_number_the_student_typed_is_removed_like_in_the_question():
    question = "Is 302-857-6060 the main line?"
    assert redact_echoes("Yes, 302.857.6060 is the main line.", question) == (
        "Yes, [phone] is the main line."
    )
    assert redact(question) == "Is [phone] the main line?"


def test_an_answer_is_unchanged_when_the_question_has_no_details():
    answer = "Call 302-857-6060 or email admissions@desu.edu. Your ID is on your card."
    assert redact_echoes(answer, "How do I find my student ID?") == answer


def test_an_unrelated_number_is_not_mistaken_for_the_students():
    # Same last four digits, different number
    assert redact_echoes("Call 302-857-0199.", "text me at 302-555-0199") == "Call 302-857-0199."
