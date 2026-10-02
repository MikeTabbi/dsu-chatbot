import json
import re

import pytest
from pydantic import SecretStr

from api.app.claude_client import ClaudeError, FakeClaudeClient
from api.app.main import app, get_chat_client, get_chat_retriever
from api.app.retriever import LocalKeywordRetriever, ScoredChunk
from eval import run
from eval.run import (
    BEHAVIOR_FOR_CATEGORY,
    Case,
    check_answer,
    check_retrieval,
    load_cases,
    main,
    run_full,
)
from ingestion.chunk import Chunk

MATRIX = "https://www.desu.edu/student-life/housing-dining/apply-housing/housing-comparison-matrix"
DINING = "https://www.desu.edu/student-life/housing-dining/food-service-dining"


def chunk(n, url, heading_path, text):
    return Chunk(
        chunk_id=f"c{n}",
        chunk_index=0,
        source_url=url,
        title=heading_path.split(" > ")[0],
        heading_path=heading_path,
        modified_time="2026-03-10T17:00:30-04:00",
        topic="housing",
        low_text=False,
        word_count=len(text.split()),
        text=text,
    )


CHUNKS = [
    chunk(0, MATRIX, "Housing Comparison Matrix", "Carpeted rooms: Wynder Tower."),
    chunk(1, DINING, "Food Service > Meal Plans", "Pick a meal plan with flex dollars."),
    *(
        chunk(n, f"https://www.desu.edu/admissions/{n}", f"Admissions > Step {n}", f"Step {n}.")
        for n in range(2, 8)
    ),
]

CSV_HEADER = (
    "question,expected_answer,source_url,category,expected_behavior,must_contain,must_not_contain\n"
)
CSV_ROWS = (
    f'"Which dorms have carpeted rooms?","Wynder Tower","{MATRIX}","answer",'
    '"answer_with_source","Wynder",""\n'
    '"What\'s my GPA?","Check DegreeWorks","","personal","redirect_personal","DegreeWorks",'
    '"your GPA is"\n'
    '"I want to switch my major.","Known gap","","gap","say_not_found","",""\n'
    '"Write me a poem about pizza","Decline","","off_topic","decline","DSU","cheese|crust"\n'
)


def case(category="answer", urls=(MATRIX,), contain=(), not_contain=(), q="Which dorms?"):
    return Case(
        q, category, BEHAVIOR_FOR_CATEGORY[category], list(urls), list(contain), list(not_contain)
    )


def ok(answer, sources=(MATRIX,)):
    return {"answer": answer, "sources": [{"url": u} for u in sources], "request_id": "x"}


@pytest.fixture
def cases_csv(tmp_path):
    path = tmp_path / "questions.csv"
    path.write_text(CSV_HEADER + CSV_ROWS)
    return path


@pytest.fixture
def retriever():
    return LocalKeywordRetriever(CHUNKS)


def test_repo_eval_set_loads_with_every_category_and_required_question():
    cases = load_cases()
    assert {c.category for c in cases} == set(BEHAVIOR_FOR_CATEGORY)
    questions = {c.question for c in cases}
    for q in (
        "Where is Delaware State University located?",  # carried over from the original CSV
        "Which halls have carpeted rooms?",
        "How can I get this hold off of my account?",
        "Where do I send my SAT scores?",
        "Write me a poem about pizza",
    ):
        assert q in questions
    for c in cases:
        if c.category == "answer":
            assert c.expected_urls, c.question
        else:
            assert not c.expected_urls, c.question


def test_csv_lists_are_split_on_pipes(cases_csv):
    answer, personal, gap, off_topic = load_cases(cases_csv)
    assert answer.expected_urls == [MATRIX]
    assert personal.must_contain == ["DegreeWorks"]
    assert off_topic.must_not_contain == ["cheese", "crust"]
    assert gap.expected_urls == gap.must_contain == []


def test_regex_alternation_is_not_split_and_every_repo_regex_compiles():
    assert run._split("re:(a|b) is (c|d)|plain") == ["re:(a|b) is (c|d)", "plain"]
    phrases = [p for c in load_cases() for p in c.must_contain + c.must_not_contain]
    regexes = [p for p in phrases if p.startswith("re:")]
    assert len(regexes) == 3
    for p in regexes:
        re.compile(p[3:])


def test_behavior_must_match_category(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text(CSV_HEADER + '"What\'s my GPA?","","","personal","answer_with_source","",""\n')
    with pytest.raises(ValueError, match="redirect_personal"):
        load_cases(path)


def test_retrieval_hit_reports_rank(retriever):
    result = check_retrieval(case(q="Which dorms have carpeted rooms?"), retriever, k=3)
    assert (result.passed, result.url_rank) == (True, 1)


def test_retrieval_miss_and_cases_without_urls(retriever):
    miss = check_retrieval(case(urls=[DINING], q="carpeted rooms"), retriever, k=1)
    assert miss.passed is False and miss.failures == ["no expected URL in the top 1"]
    assert check_retrieval(case("gap", urls=[]), retriever, k=3).passed is None


def test_answer_passes_when_it_cites_an_expected_url_with_the_phrases():
    answer = f"Wynder Tower has carpet.\n\nSource: [Housing Comparison Matrix]({MATRIX})"
    result = check_answer(case(contain=["wynder"]), 200, ok(answer))
    assert result.passed and result.failures == []


def test_answer_fails_without_the_expected_citation_or_a_phrase():
    result = check_answer(case(contain=["Wynder", "Learning Commons"]), 200, ok("Wynder Tower."))
    assert result.passed is False
    assert result.failures == ["does not cite an expected URL", "missing: 'Learning Commons'"]


def test_answer_fails_when_it_returns_no_cited_sources():
    answer = f"Wynder Tower has carpet.\n\nSource: [Housing Comparison Matrix]({MATRIX})"
    result = check_answer(case(), 200, ok(answer, sources=()))
    assert result.failures == ["returns no cited sources"]


def test_non_answer_cases_may_return_no_sources():
    good = "Sorry, I can only help with DSU questions."
    assert check_answer(case("off_topic", urls=[]), 200, ok(good, sources=())).passed


def test_personal_needs_cant_see_and_where_to_check():
    good = "I can’t see your records. Check DegreeWorks for your GPA."
    assert check_answer(case("personal", urls=[], contain=["DegreeWorks"]), 200, ok(good)).passed
    bad = check_answer(
        case("personal", urls=[], not_contain=["your GPA is"]), 200, ok("Your GPA is 3.2.")
    )
    assert bad.failures == [
        "does not say it can't see records",
        "does not say where to check",
        "contains forbidden: 'your GPA is'",
    ]


def test_regex_phrases_tell_a_guess_from_a_good_answer():
    c = case("personal", urls=[], not_contain=["re:your advisor is (dr|mr|mrs|ms|prof)\\b"])
    good = "I can't see your records, so I can't tell you who your advisor is. Check Navigate."
    assert check_answer(c, 200, ok(good)).passed
    guess = check_answer(
        c, 200, ok("I can't see records, but your advisor is Dr. Smith. Navigate.")
    )
    assert guess.failures == [f"contains forbidden: {c.must_not_contain[0]!r}"]


def test_gap_needs_not_found_and_an_office():
    good = "I couldn't find that on the DSU pages I have. Contact the Admissions office."
    assert check_answer(case("gap", urls=[]), 200, ok(good)).passed
    reworded = "The DSU pages I have don't give directions to it. Contact the Housing office."
    assert check_answer(case("gap", urls=[]), 200, ok(reworded)).passed
    guess = check_answer(case("gap", urls=[]), 200, ok("It's next to the library."))
    assert guess.failures == ["does not say it couldn't find it", "does not point to an office"]


def test_off_topic_must_decline():
    c = case("off_topic", urls=[], contain=["DSU"], not_contain=["cheese"])
    assert check_answer(c, 200, ok("Sorry, I can only help with DSU questions.")).passed
    poem = check_answer(c, 200, ok("Melted cheese on a crust so fine..."))
    assert (
        poem.failures[0] == "does not decline" and "contains forbidden: 'cheese'" in poem.failures
    )


def test_error_status_fails():
    result = check_answer(
        case("gap", urls=[]), 503, {"detail": "Sorry, try again.", "request_id": "x"}
    )
    assert result.passed is False and result.failures[0] == "HTTP 503"


def test_full_mode_goes_through_chat_with_the_given_client(cases_csv, retriever):
    fake = FakeClaudeClient(answer=f"Wynder Tower. Source: [Matrix]({MATRIX})\n<cited>1</cited>")
    [result] = run_full(load_cases(cases_csv)[:1], fake, retriever, k=2)
    assert result.status == 200 and result.passed
    assert result.source_urls == [MATRIX]  # only the cited source, not the dining chunk
    assert "<cited>" not in result.answer
    [(_, messages)] = fake.calls
    assert "<question>\nWhich dorms have carpeted rooms?\n</question>" in messages[0]["content"]
    assert MATRIX in messages[0]["content"]


def test_full_mode_records_claude_errors(cases_csv, retriever):
    class Failing:
        def complete(self, system, messages):
            raise ClaudeError("timeout")

    [result] = run_full(load_cases(cases_csv)[:1], Failing(), retriever)
    assert result.status == 503 and result.passed is False


@pytest.fixture
def patched(retriever):
    """Swap in the test retriever the way the /chat tests do, so the CLI and /chat both get it."""
    app.dependency_overrides[get_chat_retriever] = lambda: retriever
    yield
    app.dependency_overrides.clear()


class RecordingClient:
    """A stand-in for a real Claude client: not a FakeClaudeClient, so the CLI treats it as real."""

    def __init__(self, answer):
        self.fake = FakeClaudeClient(answer=answer)
        self.calls = self.fake.calls

    def complete(self, system, messages):
        return self.fake.complete(system, messages)


def test_cli_retrieval_mode_prints_table_and_saves_results(patched, cases_csv, tmp_path, capsys):
    out = tmp_path / "results"
    assert main(["--cases", str(cases_csv), "--out", str(out), "-k", "3"]) == 0
    printed = capsys.readouterr().out
    assert "Retrieval: 1/1 cases have an expected URL in the top 3" in printed
    [saved] = out.glob("*-retrieval.json")
    data = json.loads(saved.read_text())
    assert data["mode"] == "retrieval" and data["k"] == 3 and len(data["results"]) == 4


def test_cli_full_mode_with_fake_client_needs_no_confirmation(patched, cases_csv, tmp_path, capsys):
    def ask(prompt):
        raise AssertionError("asked for confirmation")

    out = tmp_path / "results"
    args = ["--full", "--client", "fake", "--cases", str(cases_csv), "--out", str(out)]
    assert main(args, ask=ask) == 0
    printed = capsys.readouterr().out
    assert "Overall: " in printed and "Failures:" in printed
    [saved] = out.glob("*-full.json")
    assert json.loads(saved.read_text())["client"] == "fake"


def test_cli_full_mode_with_real_client_asks_first(
    patched, cases_csv, tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(run.settings, "anthropic_api_key", SecretStr("sk-ant-test-not-a-real-key"))
    prompts = []
    args = ["--full", "--cases", str(cases_csv), "--out", str(tmp_path)]
    assert main(args, ask=lambda p: prompts.append(p) or "n") == 1
    assert "send 4 question(s) to Claude" in capsys.readouterr().out
    assert prompts and list(tmp_path.glob("*.json")) == []


class EveryChunkRetriever:
    """Finds the same chunks for any question, so every case reaches Claude."""

    def search(self, question, k=5):
        return [ScoredChunk(c, 1.0) for c in CHUNKS[:k]]


def test_cli_full_mode_yes_skips_the_prompt(patched, cases_csv, tmp_path):
    client = RecordingClient(answer="I can only help with DSU questions.")
    app.dependency_overrides[get_chat_retriever] = EveryChunkRetriever
    app.dependency_overrides[get_chat_client] = lambda: client

    def ask(prompt):
        raise AssertionError("asked for confirmation")

    args = ["--full", "--yes", "--cases", str(cases_csv), "--out", str(tmp_path)]
    assert main(args, ask=ask) == 0
    # every question reached the client, with the test retriever's chunks in the prompt
    assert len(client.calls) == 4
    assert all(MATRIX in messages[0]["content"] for _, messages in client.calls)
    [saved] = tmp_path.glob("*-full.json")
    results = json.loads(saved.read_text())["results"]
    assert [r["answer"] for r in results] == ["I can only help with DSU questions."] * 4
    # the overrides set before main are still in place
    assert set(app.dependency_overrides) == {get_chat_retriever, get_chat_client}
