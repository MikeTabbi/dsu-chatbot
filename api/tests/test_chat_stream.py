"""POST /chat/stream: the answer as server-sent events, with the same steps, limits, and logging
as /chat. Fake Claude client only."""

import json
import logging
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from api.app.claude_client import ClaudeError, ClaudeReply, FakeClaudeClient
from api.app.config import Settings
from api.app.main import (
    BUSY_ANSWER,
    NO_CONTENT_ANSWER,
    TOO_FAST,
    UNAVAILABLE_ANSWER,
    app,
    get_chat_client,
    get_chat_exchange_log,
    get_chat_rate_limiter,
    get_chat_reranker,
    get_chat_retriever,
    get_settings,
)
from api.app.prompt import CitationFilter
from api.app.rate_limit import InMemoryRateLimiter
from api.app.request_guard import NOT_JSON
from api.app.retriever import LocalKeywordRetriever
from api.tests.test_chat import CHUNKS, QUESTION

ANSWER = "Tubman-Lawson and Warren-Franklin have carpet. See [the halls](https://www.desu.edu)."
REPLY = f"{ANSWER}\n\n<cited>1, 2</cited>"
NOON = datetime(2026, 10, 2, 12, 0, 15, tzinfo=UTC).timestamp()


class RecordingLog:
    def __init__(self):
        self.exchanges = []

    def add(self, exchange):
        self.exchanges.append(exchange)

    def set_feedback(self, request_id, rating, comment):
        pass


class ScriptedClient:
    """Streams the given pieces, then raises `error` (if any) or sends the reply."""

    model = "scripted"

    def __init__(self, pieces, error=None):
        self.pieces = pieces
        self.error = error
        self.calls = 0

    def stream(self, system, messages):
        self.calls += 1
        yield from self.pieces
        if self.error:
            raise self.error
        yield ClaudeReply("".join(self.pieces).strip(), self.model, 10, 5)


@pytest.fixture
def fake():
    return FakeClaudeClient(answer=REPLY, chunk_chars=3)


@pytest.fixture
def exchanges():
    return RecordingLog()


@pytest.fixture
def settings():
    return dict(
        chat_top_k=3,
        chat_max_question_chars=50,
        rate_limit_chat_per_minute=3,
        claude_daily_call_budget=100,
    )


@pytest.fixture
def client(fake, exchanges, settings):
    limiter = InMemoryRateLimiter(lambda: NOON)
    app.dependency_overrides[get_chat_retriever] = lambda: LocalKeywordRetriever(CHUNKS)
    app.dependency_overrides[get_chat_reranker] = lambda: None
    app.dependency_overrides[get_chat_client] = lambda: fake
    app.dependency_overrides[get_chat_exchange_log] = lambda: exchanges
    app.dependency_overrides[get_chat_rate_limiter] = lambda: limiter
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None, **settings)
    yield TestClient(app)
    app.dependency_overrides.clear()


def events(response):
    """The (event, data) pairs of a server-sent event stream."""
    assert response.headers["content-type"].startswith("text/event-stream")
    out = []
    for block in response.text.split("\n\n"):
        if not block.strip():
            continue
        lines = dict(line.split(": ", 1) for line in block.split("\n"))
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def stream(client, question=QUESTION, **body):
    return client.post("/chat/stream", json={"question": question, **body})


def use(client_object):
    app.dependency_overrides[get_chat_client] = lambda: client_object


# The citation marker filter -------------------------------------------------------------------


def shown(pieces):
    f = CitationFilter()
    out = [f.feed(p) for p in pieces]
    return out, "".join(out) + f.flush()


@pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 100])
def test_marker_is_never_shown_whatever_the_piece_size(size):
    text = "Halls have carpet.\n\n<cited>1, 3</cited>"
    pieces = [text[i : i + size] for i in range(0, len(text), size)]
    out, whole = shown(pieces)
    assert whole == "Halls have carpet.\n\n"
    for piece in out:
        assert "<" not in piece and "cited" not in piece


def test_marker_split_at_every_point_is_never_shown():
    text = "Carpet.<CITED>2</Cited>"
    for i in range(len(text) + 1):
        for j in range(i, len(text) + 1):
            out, whole = shown([text[:i], text[i:j], text[j:]])
            assert whole == "Carpet."
            assert all("<" not in piece for piece in out), (i, j)


def test_a_marker_in_the_middle_is_dropped_and_the_text_after_it_shown():
    _, whole = shown(["Tubman<ci", "ted>1</cited> has carpet. </cit", "ed>Done"])
    assert whole == "Tubman has carpet. Done"


def test_text_that_only_looks_like_a_tag_start_is_shown_once_it_isnt_one():
    out, whole = shown(["5 <", " 6 and <b>bold</b> <ci", "ty>"])
    assert out[0] == "5 "  # "<" waits for the next piece
    assert out[1] == "< 6 and <b>bold</b> "
    assert whole == "5 < 6 and <b>bold</b> <city>"


def test_held_back_text_is_shown_at_the_end_but_an_open_marker_never_is():
    assert shown(["Ends with <c"])[1] == "Ends with <c"
    assert shown(["Cut off <cited>1, "])[1] == "Cut off "


# Streaming an answer -------------------------------------------------------------------------


def test_streams_the_answer_then_a_final_event_like_chat(client, fake):
    response = stream(client)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache, no-transform"
    assert response.headers["x-accel-buffering"] == "no"
    got = events(response)
    deltas = [data["text"] for name, data in got if name == "delta"]
    assert len(deltas) > 10  # it really came in pieces
    assert "".join(deltas).strip() == ANSWER
    for text in deltas:
        assert "<" not in text and "cited" not in text.lower()

    name, done = got[-1]
    assert name == "done"
    assert [n for n, _ in got].count("done") == 1
    assert done["answer"] == ANSWER
    assert done["outcome"] == "answered"
    assert len(done["request_id"]) == 32
    regular = client.post("/chat", json={"question": QUESTION}).json()
    assert done["sources"] == regular["sources"]
    assert [s["url"] for s in done["sources"]]  # the cited ones only


def test_the_same_prompt_as_chat(client, fake):
    stream(client)
    client.post("/chat", json={"question": QUESTION})
    assert fake.calls[0] == fake.calls[1]


def test_a_marker_split_across_pieces_never_reaches_the_student(client):
    use(ScriptedClient(["Carpet in Tubman.", "\n\n<", "ci", "ted", ">", "1", "</c", "ited>"]))
    got = events(stream(client))
    streamed = "".join(d["text"] for n, d in got if n == "delta")
    assert streamed == "Carpet in Tubman.\n\n"
    assert got[-1] == ("done", {**got[-1][1], "answer": "Carpet in Tubman."})
    assert got[-1][1]["sources"]


def test_no_content_is_a_final_event_without_calling_claude(client, fake, exchanges):
    got = events(stream(client, "What's the wifi password?"))
    assert [n for n, _ in got] == ["done"]
    assert got[0][1]["answer"] == NO_CONTENT_ANSWER
    assert got[0][1]["sources"] == []
    assert got[0][1]["outcome"] == "no_sources"
    assert fake.calls == []
    assert [e.outcome for e in exchanges.exchanges] == ["no_sources"]


def test_a_not_found_answer_says_so_in_the_final_event(client, fake):
    fake.answer = "I couldn't find that in the DSU information I have.\n<cited></cited>"
    assert events(stream(client))[-1][1]["outcome"] == "not_found"


# Errors -------------------------------------------------------------------------------------


def assert_friendly(response, got):
    name, data = got[-1]
    assert name == "error"
    assert data["detail"] == UNAVAILABLE_ANSWER
    assert len(data["request_id"]) == 32
    for internal in ("Traceback", "ClaudeError", "RuntimeError", "internal.example", "overloaded"):
        assert internal not in response.text


def test_an_error_mid_stream_ends_with_a_friendly_error_event(client, exchanges, caplog):
    use(ScriptedClient(["Tubman-Lawson ", "has "], ClaudeError("api_error", "overloaded")))
    with caplog.at_level(logging.INFO, logger="api.app.main"):
        response = stream(client)
    got = events(response)
    assert [n for n, _ in got] == ["delta", "delta", "error"]
    assert_friendly(response, got)
    assert "outcome=claude_error reason=api_error" in caplog.text
    [exchange] = exchanges.exchanges
    assert (exchange.outcome, exchange.answer) == ("error", UNAVAILABLE_ANSWER)


def test_an_error_before_any_text_ends_with_a_friendly_error_event(client):
    use(ScriptedClient([], ClaudeError("timeout", "APITimeoutError at https://internal.example")))
    response = stream(client)
    got = events(response)
    assert [n for n, _ in got] == ["error"]
    assert_friendly(response, got)


def test_an_unexpected_error_mid_stream_is_friendly_and_its_type_is_logged(client, caplog):
    use(ScriptedClient(["Tubman "], RuntimeError("secret detail at internal.example")))
    with caplog.at_level(logging.INFO, logger="api.app.main"):
        response = stream(client)
    assert_friendly(response, events(response))
    assert "outcome=stream_error reason=RuntimeError" in caplog.text
    assert "secret detail" not in caplog.text


def test_an_unexpected_error_before_the_answer_is_a_friendly_error_event(client):
    class BrokenRetriever:
        def search(self, question, k=5):
            raise RuntimeError("index at internal.example is down")

    app.dependency_overrides[get_chat_retriever] = BrokenRetriever
    response = stream(client)
    assert response.status_code == 200
    assert_friendly(response, events(response))


def test_a_stream_that_ends_without_a_reply_is_an_error(client):
    class NoReply:
        def stream(self, system, messages):
            yield "Tubman"

    use(NoReply())
    assert events(stream(client))[-1][0] == "error"


# Logging, limits, and the request checks ----------------------------------------------------


def test_the_exchange_is_logged_once_after_the_stream_redacted(client, fake, exchanges, caplog):
    question = "carpet? I'm jo@example.com"
    fake.answer = "Jo at jo@example.com: Tubman has carpet.\n<cited>1</cited>"
    with caplog.at_level(logging.INFO, logger="api.app.main"):
        done = events(stream(client, question))[-1][1]
    [exchange] = exchanges.exchanges
    assert exchange.request_id == done["request_id"]
    assert exchange.outcome == "answered"
    assert exchange.question == "carpet? I'm [email]"
    assert exchange.answer == "Jo at [email]: Tubman has carpet."
    assert exchange.model == "fake" and exchange.output_tokens
    assert exchange.source_urls == [s["url"] for s in done["sources"]]
    assert f"chat request_id={done['request_id']} chunks=3" in caplog.text
    assert "jo@example.com" not in caplog.text


def test_shares_the_per_client_limit_with_chat(client, fake):
    assert stream(client).status_code == 200
    assert client.post("/chat", json={"question": QUESTION}).status_code == 200
    assert stream(client).status_code == 200
    response = stream(client)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "45"
    assert response.json()["detail"] == TOO_FAST
    assert len(fake.calls) == 3


def test_the_daily_budget_is_a_503_like_chat(client, fake, settings):
    settings["claude_daily_call_budget"] = 1
    assert stream(client).status_code == 200
    response = stream(client)
    assert response.status_code == 503
    assert response.json()["detail"] == BUSY_ANSWER
    assert int(response.headers["retry-after"]) > 0
    assert len(fake.calls) == 1


def test_bad_questions_are_refused_like_chat(client, fake):
    assert stream(client, "   ").json() == {"detail": "Please enter a question."}
    assert stream(client, "carpet " * 10).status_code == 422
    response = client.post("/chat/stream", content=b"question", headers={"Content-Type": "text"})
    assert (response.status_code, response.json()["detail"]) == (415, NOT_JSON)
    assert fake.calls == []


def test_follow_up_history_is_accepted(client, fake):
    history = [{"question": "Which halls have carpet?", "answer": "Tubman-Lawson."}]
    got = events(stream(client, "What about Warren?", history=history))
    assert got[-1][0] == "done"
    assert "<history>" in fake.calls[0][1][0]["content"]
