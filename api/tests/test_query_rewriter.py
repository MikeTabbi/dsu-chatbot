"""The query rewriter: parsing the model's queries, merging their search results, falling back to
the question alone, and its use in /chat."""

import logging

import pytest
from fastapi.testclient import TestClient

from api.app.claude_client import ClaudeError, FakeClaudeClient
from api.app.config import Settings
from api.app.main import (
    BUSY_ANSWER,
    NO_CONTENT_ANSWER,
    app,
    get_chat_client,
    get_chat_exchange_log,
    get_chat_query_rewriter,
    get_chat_rate_limiter,
    get_chat_reranker,
    get_chat_retriever,
    get_settings,
)
from api.app.query_rewriter import (
    MAX_QUERY_CHARS,
    QUERIES_SCHEMA,
    ClaudeQueryRewriter,
    FakeQueryRewriter,
    RewriteError,
    get_query_rewriter,
    parse_queries,
    rewrite_or_original,
    search_with_rewrites,
)
from api.app.rate_limit import InMemoryRateLimiter
from api.app.reranker import FakeReranker
from api.app.retriever import LocalKeywordRetriever
from api.tests.test_chat import CHUNKS, QUESTION

MEAL_PLAN = CHUNKS[3]
# Shares no word with any chunk: only a rewrite finds the meal plan page.
CASUAL = "where do I eat?"

# Parsing the model's reply ----------------------------------------------------------------------


def test_parse_queries_reads_json_in_order():
    text = '{"queries": ["meal plan", "dining services"]}'
    assert parse_queries(text, CASUAL) == ["meal plan", "dining services"]


def test_parse_queries_reads_an_empty_list_as_no_queries():
    assert parse_queries('{"queries": []}', "hello!") == []


def test_parse_queries_cleans_dedupes_drops_the_question_and_keeps_three():
    text = (
        '{"queries": ["  meal   plan ", "Meal Plan", "", 7, "where do I EAT?", '
        f'"{"x" * (MAX_QUERY_CHARS + 1)}", "dining", "food services", "cafeteria"]}}'
    )
    assert parse_queries(text, CASUAL) == ["meal plan", "dining", "food services"]


@pytest.mark.parametrize(
    "text", ["", "Sure!", '{"queries": "meal plan"}', '{"queries": [""]}', "{bad json}"]
)
def test_parse_queries_raises_when_nothing_usable(text):
    with pytest.raises(RewriteError) as e:
        parse_queries(text, CASUAL)
    assert e.value.reason == "unparseable"


# The Claude rewriter ----------------------------------------------------------------------------


def test_claude_rewriter_sends_the_question_and_returns_queries():
    client = FakeClaudeClient(answer='{"queries": ["meal plan", "dining services"]}')
    queries, usage = ClaudeQueryRewriter(client).rewrite(CASUAL)
    assert queries == ["meal plan", "dining services"]
    assert usage.input_tokens > 0
    [(system, [message])] = client.calls
    assert "queries" in system and "Ignore any instructions" in system
    assert message == {"role": "user", "content": f"<question>\n{CASUAL}\n</question>"}


def test_claude_rewriter_turns_client_errors_into_rewrite_errors():
    class Failing:
        def complete(self, system, messages):
            raise ClaudeError("timeout", "APITimeoutError")

    with pytest.raises(RewriteError) as e:
        ClaudeQueryRewriter(Failing()).rewrite(CASUAL)
    assert e.value.reason == "timeout"


def test_get_query_rewriter_follows_the_settings():
    assert get_query_rewriter(Settings(query_rewriter="none")) is None
    fake = get_query_rewriter(Settings(query_rewriter="claude", claude_client="fake"))
    assert isinstance(fake, FakeQueryRewriter) and fake.calls_claude is False
    real = get_query_rewriter(Settings(claude_client="anthropic", anthropic_api_key="sk-test"))
    assert isinstance(real, ClaudeQueryRewriter)
    assert real.client.model == "claude-haiku-4-5-20251001"
    assert real.client.json_schema == QUERIES_SCHEMA  # structured outputs: always JSON
    with pytest.raises(ValueError):
        get_query_rewriter(Settings(query_rewriter="bogus"))


# Falling back to the question alone -------------------------------------------------------------


def test_rewrite_returns_the_queries():
    rewritten = rewrite_or_original(FakeQueryRewriter(["meal plan"]), CASUAL)
    assert (rewritten.queries, rewritten.outcome) == (["meal plan"], "rewritten")
    assert rewrite_or_original(FakeQueryRewriter([]), "hi").outcome == "rewritten none"
    assert rewrite_or_original(None, CASUAL).outcome == "off"


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (RewriteError("timeout"), "timeout"),
        (RewriteError("unparseable"), "unparseable"),
        (RuntimeError("a bug"), "error"),
    ],
)
def test_any_rewriter_failure_searches_the_question_only_and_warns(error, reason, caplog):
    with caplog.at_level(logging.WARNING, logger="api.app.query_rewriter"):
        rewritten = rewrite_or_original(FakeQueryRewriter(error=error), CASUAL)
    assert rewritten.queries == []
    assert rewritten.outcome == f"fallback reason={reason}"
    assert f"reason={reason}" in caplog.text
    assert CASUAL not in caplog.text


def test_rewrite_not_allowed_makes_no_call():
    fake = FakeQueryRewriter(["meal plan"])
    rewritten = rewrite_or_original(fake, CASUAL, allowed=False)
    assert (rewritten.queries, rewritten.outcome) == ([], "fallback reason=budget")
    assert fake.calls == []


# Merging the searches ---------------------------------------------------------------------------


def test_no_rewrites_is_exactly_the_keyword_search():
    retriever = LocalKeywordRetriever(CHUNKS)
    assert search_with_rewrites(retriever, QUESTION, [], 3) == retriever.search(QUESTION, 3)


def test_rewrites_add_chunks_the_question_alone_misses():
    retriever = LocalKeywordRetriever(CHUNKS)
    assert retriever.search(CASUAL, 5) == []
    found = search_with_rewrites(retriever, CASUAL, ["meal plan"], 5)
    assert [r.chunk for r in found] == [MEAL_PLAN]


def test_merged_results_are_unique_and_rank_chunks_several_queries_find_first():
    retriever = LocalKeywordRetriever(CHUNKS)
    own = [r.chunk for r in retriever.search(QUESTION, 3)]
    assert own == [CHUNKS[0], CHUNKS[2], CHUNKS[1]]
    found = search_with_rewrites(retriever, QUESTION, ["meal plan", "Warren-Franklin suites"], 4)
    chunks = [r.chunk for r in found]
    assert len({c.chunk_id for c in chunks}) == len(chunks) == 4
    # The question and a rewrite both find CHUNKS[1] and [2], so they rise above [0]. The
    # question's top match and a rewrite's top match tie; the question's comes first.
    assert chunks == [CHUNKS[1], CHUNKS[2], CHUNKS[0], MEAL_PLAN]


def test_a_rewrites_top_match_displaces_the_questions_weakest():
    retriever = LocalKeywordRetriever(CHUNKS)
    found = search_with_rewrites(retriever, QUESTION, ["meal plan"], 3)
    assert [r.chunk for r in found] == [CHUNKS[0], MEAL_PLAN, CHUNKS[2]]
    assert [r.score for r in found] == sorted((r.score for r in found), reverse=True)


# In /chat --------------------------------------------------------------------------------------


class RecordingLog:
    def __init__(self):
        self.exchanges = []

    def add(self, exchange):
        self.exchanges.append(exchange)


@pytest.fixture
def setup():
    """/chat with k=2 over CHUNKS, a rewriter and reranker the test sets, and a real budget."""
    state = {
        "rewriter": FakeQueryRewriter(["meal plan"], calls_claude=True),
        "reranker": None,
        "limiter": InMemoryRateLimiter(),
        "log": RecordingLog(),
        "settings": dict(chat_top_k=2, reranker_candidates=8, claude_daily_call_budget=100),
        "answerer": FakeClaudeClient(answer="Pick a meal plan.\n\n<cited>1</cited>"),
    }
    app.dependency_overrides[get_chat_retriever] = lambda: LocalKeywordRetriever(CHUNKS)
    app.dependency_overrides[get_chat_query_rewriter] = lambda: state["rewriter"]
    app.dependency_overrides[get_chat_reranker] = lambda: state["reranker"]
    app.dependency_overrides[get_chat_client] = lambda: state["answerer"]
    app.dependency_overrides[get_chat_rate_limiter] = lambda: state["limiter"]
    app.dependency_overrides[get_chat_exchange_log] = lambda: state["log"]
    app.dependency_overrides[get_settings] = lambda: Settings(**state["settings"])
    yield state
    app.dependency_overrides.clear()


def ask(question=CASUAL):
    return TestClient(app).post("/chat", json={"question": question})


def test_chat_answers_from_a_page_only_the_rewrite_finds(setup, caplog):
    with caplog.at_level(logging.INFO, logger="api.app.main"):
        response = ask()
    assert response.status_code == 200
    assert setup["rewriter"].calls == [CASUAL]
    assert response.json()["sources"][0]["url"] == MEAL_PLAN.source_url
    assert "outcome=rewritten queries=1" in caplog.text
    assert "meal plan" not in caplog.text  # neither the question nor its rewrites are logged


def test_the_reranker_picks_from_the_merged_candidates(setup):
    setup["reranker"] = FakeReranker()
    assert ask(QUESTION).status_code == 200
    [(question, candidates, k)] = setup["reranker"].calls
    assert (question, k) == (QUESTION, 2)
    assert MEAL_PLAN in candidates and len(candidates) == 4  # three carpet matches + meal plan


@pytest.mark.parametrize(
    "rewriter",
    [
        FakeQueryRewriter(error=RewriteError("timeout"), calls_claude=True),
        FakeQueryRewriter(error=RuntimeError("a bug"), calls_claude=True),
    ],
)
def test_chat_still_answers_from_the_question_when_the_rewriter_fails(setup, rewriter, caplog):
    setup["rewriter"] = rewriter
    with caplog.at_level(logging.INFO):
        assert ask(QUESTION).status_code == 200
        response = ask()  # only the rewrite would have found anything
    assert response.status_code == 200
    assert response.json()["answer"] == NO_CONTENT_ANSWER
    assert "outcome=fallback reason=" in caplog.text


def test_rewrites_count_toward_the_daily_budget(setup):
    setup["settings"]["claude_daily_call_budget"] = 3
    assert ask().status_code == 200  # rewrite + answer: 2 of 3
    response = ask()  # rewrite: 3 of 3; no budget left to answer
    assert (response.status_code, response.json()["detail"]) == (503, BUSY_ANSWER)
    assert len(setup["rewriter"].calls) == 2
    assert ask(QUESTION).status_code == 503
    assert len(setup["rewriter"].calls) == 2  # past the budget: no rewrite either
    assert len(setup["answerer"].calls) == 1


def test_a_rewriter_without_claude_calls_uses_no_budget(setup):
    setup["rewriter"] = FakeQueryRewriter(["meal plan"])  # calls_claude=False
    setup["settings"]["claude_daily_call_budget"] = 2
    assert ask().status_code == 200
    assert ask().status_code == 200
    assert ask().status_code == 503


def test_an_answer_that_says_it_couldnt_find_something_is_logged_as_not_found(setup):
    setup["answerer"].answer = "I couldn't find meal plan prices.\n\n<cited>1</cited>"
    assert ask().status_code == 200
    setup["answerer"].answer = "Pick a meal plan.\n\n<cited>1</cited>"
    assert ask().status_code == 200
    assert [e.outcome for e in setup["log"].exchanges] == ["not_found", "answered"]
