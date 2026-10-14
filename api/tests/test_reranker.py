"""The reranker: parsing the model's IDs, falling back to keyword order, and its use in /chat."""

import logging

import pytest
from fastapi.testclient import TestClient

from api.app.claude_client import ClaudeError, FakeClaudeClient
from api.app.config import Settings
from api.app.main import (
    BUSY_ANSWER,
    app,
    get_chat_client,
    get_chat_exchange_log,
    get_chat_rate_limiter,
    get_chat_reranker,
    get_chat_retriever,
    get_settings,
)
from api.app.rate_limit import InMemoryRateLimiter
from api.app.reranker import (
    IDS_SCHEMA,
    PREVIEW_WORDS,
    ClaudeReranker,
    FakeReranker,
    RerankError,
    get_reranker,
    parse_ids,
    preview,
    rerank_or_keyword,
)
from api.app.retriever import LocalKeywordRetriever, ScoredChunk
from api.tests.test_chat import CHUNKS, QUESTION, chunk

MATRIX = 2  # CHUNKS[2] is the comparison matrix


def scored(chunks):
    return [ScoredChunk(c, 1.0 - n / 100) for n, c in enumerate(chunks)]


# Parsing the model's reply ----------------------------------------------------------------------


def test_parse_ids_reads_json_and_numbers_from_one():
    assert parse_ids('{"ids": [3, 1, 2]}', count=5, k=5) == [2, 0, 1]


def test_parse_ids_reads_an_empty_list_as_none_relevant():
    assert parse_ids('{"ids": []}\n\nNone of these are about pizza.', count=5, k=5) == []


def test_parse_ids_tolerates_text_around_the_json_and_a_bare_list():
    assert parse_ids('Here you go:\n```json\n{"ids": [2]}\n```', count=5, k=5) == [1]
    assert parse_ids("[4, 5]", count=5, k=5) == [3, 4]


def test_parse_ids_drops_unknown_duplicate_and_non_integer_ids_and_keeps_k():
    assert parse_ids('{"ids": [9, 0, 2, 2, "3", true, 1.0, 4, 1, 5]}', count=5, k=3) == [1, 3, 0]


@pytest.mark.parametrize("text", ["", "Sure!", '{"ids": "1, 2"}', '{"ids": [99]}', "{bad json}"])
def test_parse_ids_raises_when_nothing_usable(text):
    with pytest.raises(RerankError) as e:
        parse_ids(text, count=5, k=3)
    assert e.value.reason == "unparseable"


def test_preview_has_title_heading_url_and_the_first_words_without_link_urls():
    long = chunk(0, "Housing > Facts", "See [the matrix](https://x.example/m) " + "word " * 100)
    text = preview(long)
    assert text.startswith("Housing & Dining | Housing > Facts | https://www.desu.edu/")
    assert "the matrix" in text and "x.example" not in text
    assert len(text.split("\n", 1)[1].split()) == PREVIEW_WORDS


# The Claude reranker ----------------------------------------------------------------------------


def test_claude_reranker_sends_previews_and_returns_the_models_order():
    client = FakeClaudeClient(answer='{"ids": [3, 1]}')
    ids, usage = ClaudeReranker(client).rerank(QUESTION, CHUNKS[:4], k=2)
    assert ids == [2, 0]
    assert usage.input_tokens > 0
    [(system, [message])] = client.calls
    assert "ids" in system
    assert QUESTION in message["content"]
    assert '<candidate id="3">' in message["content"]
    assert "Housing Comparison Matrix" in message["content"]
    assert message["content"].endswith("up to 2 ids.")


def test_claude_reranker_turns_client_errors_into_rerank_errors():
    class Failing:
        def complete(self, system, messages):
            raise ClaudeError("timeout", "APITimeoutError")

    with pytest.raises(RerankError) as e:
        ClaudeReranker(Failing()).rerank(QUESTION, CHUNKS, k=3)
    assert e.value.reason == "timeout"


def test_get_reranker_follows_the_settings():
    assert get_reranker(Settings(reranker="none")) is None
    assert isinstance(get_reranker(Settings(reranker="claude", claude_client="fake")), FakeReranker)
    real = get_reranker(Settings(claude_client="anthropic", anthropic_api_key="sk-test"))
    assert isinstance(real, ClaudeReranker)
    assert real.client.model == "claude-haiku-4-5-20251001"
    assert real.client.json_schema == IDS_SCHEMA  # structured outputs: the reply is always JSON
    with pytest.raises(ValueError):
        get_reranker(Settings(reranker="bogus"))


# Falling back to keyword order -----------------------------------------------------------------


def test_rerank_puts_the_chosen_chunks_first_and_fills_up_to_k_in_keyword_order():
    candidates = scored(CHUNKS)
    reranked = rerank_or_keyword(FakeReranker(order=[MATRIX]), QUESTION, candidates, k=3)
    assert reranked.outcome == "reranked"
    assert [r.chunk for r in reranked.results] == [CHUNKS[2], CHUNKS[0], CHUNKS[1]]


def test_reranker_finding_nothing_relevant_keeps_keyword_order_without_a_warning(caplog):
    candidates = scored(CHUNKS)
    with caplog.at_level(logging.WARNING):
        reranked = rerank_or_keyword(FakeReranker(order=[]), QUESTION, candidates, k=3)
    assert (reranked.results, reranked.outcome) == (candidates[:3], "reranked none_relevant")
    assert caplog.text == ""


def test_no_reranker_or_nothing_to_choose_between_keeps_keyword_order():
    candidates = scored(CHUNKS)
    assert rerank_or_keyword(None, QUESTION, candidates, k=3).results == candidates[:3]
    fake = FakeReranker(order=[1, 0])
    reranked = rerank_or_keyword(fake, QUESTION, candidates[:3], k=3)
    assert (reranked.results, reranked.outcome, fake.calls) == (candidates[:3], "keyword", [])


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (RerankError("timeout"), "timeout"),
        (RerankError("unparseable", "Sure!"), "unparseable"),
        (RuntimeError("a bug"), "error"),
    ],
)
def test_any_reranker_failure_keeps_keyword_order_and_warns(error, reason, caplog):
    candidates = scored(CHUNKS)
    with caplog.at_level(logging.WARNING, logger="api.app.reranker"):
        reranked = rerank_or_keyword(FakeReranker(error=error), QUESTION, candidates, k=3)
    assert reranked.results == candidates[:3]
    assert reranked.outcome == f"fallback reason={reason}"
    assert f"reason={reason}" in caplog.text


def test_rerank_not_allowed_keeps_keyword_order_without_a_call():
    fake = FakeReranker(order=[MATRIX])
    reranked = rerank_or_keyword(fake, QUESTION, scored(CHUNKS), k=3, allowed=False)
    assert reranked.outcome == "fallback reason=budget"
    assert fake.calls == []


# In /chat --------------------------------------------------------------------------------------


class PicksTheMatrix(FakeReranker):
    """Puts the comparison matrix first, wherever keyword search ranked it."""

    def rerank(self, question, candidates, k):
        urls = [c.source_url for c in candidates]
        self.order = [urls.index(CHUNKS[MATRIX].source_url)]
        return super().rerank(question, candidates, k)


class NoLog:
    def add(self, exchange):
        pass


@pytest.fixture
def setup():
    """/chat with k=2 over CHUNKS (three match QUESTION), a reranker the test sets, and a real
    (in-memory) budget."""
    state = {
        "reranker": PicksTheMatrix(calls_claude=True),
        "limiter": InMemoryRateLimiter(),
        "settings": dict(chat_top_k=2, reranker_candidates=8, claude_daily_call_budget=100),
    }
    answerer = FakeClaudeClient(answer="The matrix lists carpet.\n\n<cited>1</cited>")
    state["answerer"] = answerer
    app.dependency_overrides[get_chat_retriever] = lambda: LocalKeywordRetriever(CHUNKS)
    app.dependency_overrides[get_chat_reranker] = lambda: state["reranker"]
    app.dependency_overrides[get_chat_client] = lambda: answerer
    app.dependency_overrides[get_chat_rate_limiter] = lambda: state["limiter"]
    app.dependency_overrides[get_chat_exchange_log] = NoLog
    app.dependency_overrides[get_settings] = lambda: Settings(**state["settings"])
    yield state
    app.dependency_overrides.clear()


def ask(question=QUESTION):
    return TestClient(app).post("/chat", json={"question": question})


def test_chat_answers_from_the_reranked_chunks(setup):
    response = ask()
    assert response.status_code == 200
    [(question, candidates, k)] = setup["reranker"].calls
    assert (question, k) == (QUESTION, 2)
    assert len(candidates) == 3  # every match, not just the top k
    [(_, [message])] = setup["answerer"].calls
    first_source = message["content"].split('<source id="1">', 1)[1].split("</source>")[0]
    assert "Carpeted: Tubman-Lawson, Warren-Franklin." in first_source
    assert response.json()["sources"][0]["url"] == CHUNKS[MATRIX].source_url


def test_chat_still_answers_when_the_reranker_fails(setup, caplog):
    setup["reranker"] = FakeReranker(error=RerankError("timeout"), calls_claude=True)
    with caplog.at_level(logging.INFO):
        response = ask()
    assert response.status_code == 200
    assert "outcome=fallback reason=timeout" in caplog.text


def test_reranks_count_toward_the_daily_budget(setup):
    setup["settings"]["claude_daily_call_budget"] = 3
    assert ask().status_code == 200  # answer + rerank: 2 of 3
    assert len(setup["reranker"].calls) == 1
    assert ask().status_code == 200  # answer: 3 of 3; no budget left to rerank, keyword order
    assert len(setup["reranker"].calls) == 1
    response = ask()
    assert (response.status_code, response.json()["detail"]) == (503, BUSY_ANSWER)
    assert len(setup["answerer"].calls) == 2


def test_a_reranker_without_claude_calls_uses_no_budget(setup):
    setup["reranker"] = FakeReranker(order=[MATRIX])  # calls_claude=False
    setup["settings"]["claude_daily_call_budget"] = 2
    assert ask().status_code == 200
    assert ask().status_code == 200
    assert ask().status_code == 503
