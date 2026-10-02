"""The exchange log behind /chat, and POST /feedback."""

import logging

import pytest
from fastapi.testclient import TestClient

from api.app.claude_client import FakeClaudeClient
from api.app.config import Settings
from api.app.exchange_log import SqliteExchangeLog
from api.app.main import (
    FEEDBACK_UNAVAILABLE,
    NO_CONTENT_ANSWER,
    UNAVAILABLE_ANSWER,
    UNKNOWN_REQUEST,
    app,
    get_chat_client,
    get_chat_exchange_log,
    get_chat_retriever,
    get_settings,
)
from api.app.prompt import prompt_version
from api.app.retriever import LocalKeywordRetriever
from api.tests.test_chat import CHUNKS, HOUSING, FailingClient

QUESTION = "Which dorms have carpeted rooms? I'm jdoe@desu.edu, 302-555-0199"
ANSWER = "Tubman-Lawson has carpet.\n\n<cited>1</cited>"


class BrokenLog:
    """A store that is down."""

    def add(self, exchange):
        raise OSError("disk full")

    def set_feedback(self, request_id, rating, comment):
        raise OSError("disk full")


@pytest.fixture
def store(tmp_path):
    return SqliteExchangeLog(tmp_path / "exchanges.sqlite")


@pytest.fixture
def fake():
    return FakeClaudeClient(answer=ANSWER)


@pytest.fixture
def client(store, fake):
    app.dependency_overrides[get_chat_retriever] = lambda: LocalKeywordRetriever(CHUNKS)
    app.dependency_overrides[get_chat_client] = lambda: fake
    app.dependency_overrides[get_chat_exchange_log] = lambda: store
    app.dependency_overrides[get_settings] = lambda: Settings(
        chat_top_k=3, feedback_max_comment_chars=40
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def ask(client, question=QUESTION):
    return client.post("/chat", json={"question": question}).json()["request_id"]


def test_answered_exchange_is_logged_redacted(client, store):
    request_id = ask(client)
    [item] = store.recent()
    e = item.exchange
    assert e.request_id == request_id
    assert e.question == "Which dorms have carpeted rooms? I'm [email], [phone]"
    assert e.answer == "Tubman-Lawson has carpet."
    assert e.outcome == "answered"
    assert e.source_urls == [HOUSING]
    assert len(e.chunk_ids) == 3 and e.chunk_ids[0].startswith("c")
    assert (e.model, e.input_tokens > 0, e.output_tokens > 0) == ("fake", True, True)
    assert e.latency_ms >= 0
    assert e.prompt_version == prompt_version() and len(e.prompt_version) == 12
    assert e.timestamp.endswith("+00:00")


def test_echoed_personal_details_are_redacted_from_the_answer(client, store, fake):
    fake.answer = "We'll email jdoe@desu.edu.\n<cited>1</cited>"
    ask(client)
    assert store.recent()[0].exchange.answer == "We'll email [email]."


def test_no_sources_exchange_is_logged(client, store):
    ask(client, "What's the wifi password?")
    e = store.recent()[0].exchange
    assert (e.outcome, e.answer, e.source_urls, e.chunk_ids) == (
        "no_sources",
        NO_CONTENT_ANSWER,
        [],
        [],
    )
    assert e.model is None and e.input_tokens is None


def test_claude_error_exchange_is_logged(client, store):
    app.dependency_overrides[get_chat_client] = FailingClient
    assert client.post("/chat", json={"question": QUESTION}).status_code == 503
    e = store.recent()[0].exchange
    assert (e.outcome, e.answer, e.model) == ("error", UNAVAILABLE_ANSWER, None)


def test_chat_still_answers_when_the_log_store_fails(client, caplog):
    app.dependency_overrides[get_chat_exchange_log] = BrokenLog
    with caplog.at_level(logging.INFO):
        response = client.post("/chat", json={"question": QUESTION})
    assert response.status_code == 200
    assert response.json()["answer"] == "Tubman-Lawson has carpet."
    [warning] = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warning.getMessage().startswith("exchange not logged request_id=")
    assert "OSError" in warning.getMessage()


def test_question_is_never_in_the_app_log(client, caplog):
    app.dependency_overrides[get_chat_exchange_log] = BrokenLog
    with caplog.at_level(logging.DEBUG):
        client.post("/chat", json={"question": QUESTION})
        client.post("/feedback", json={"request_id": "x", "rating": "up", "comment": "jdoe"})
    for secret in ("carpeted", "jdoe", "302-555", "disk full"):
        assert secret not in caplog.text


def test_feedback_is_created_then_updated(client, store):
    request_id = ask(client)
    response = client.post("/feedback", json={"request_id": request_id, "rating": "up"})
    assert response.status_code == 200
    assert response.json() == {"request_id": request_id, "rating": "up"}
    assert (store.recent()[0].rating, store.recent()[0].comment) == ("up", None)

    body = {"request_id": request_id, "rating": "down", "comment": "  Wrong hall  "}
    assert client.post("/feedback", json=body).status_code == 200
    [item] = store.recent()
    assert (item.rating, item.comment) == ("down", "Wrong hall")


def test_feedback_comment_is_redacted(client, store):
    request_id = ask(client)
    body = {"request_id": request_id, "rating": "down", "comment": "text me 302-555-0199"}
    client.post("/feedback", json=body)
    assert store.recent()[0].comment == "text me [phone]"


def test_feedback_for_an_unknown_request_is_404(client):
    response = client.post("/feedback", json={"request_id": "f" * 32, "rating": "up"})
    assert response.status_code == 404
    assert response.json() == {"detail": UNKNOWN_REQUEST}


@pytest.mark.parametrize(
    "body",
    [
        {"request_id": "r", "rating": "sideways"},
        {"request_id": "r"},
        {"rating": "up"},
        {"request_id": "", "rating": "up"},
        {"request_id": "r" * 65, "rating": "up"},
    ],
)
def test_bad_feedback_is_rejected(client, body):
    assert client.post("/feedback", json=body).status_code == 422


def test_too_long_comment_is_rejected(client, store):
    request_id = ask(client)
    body = {"request_id": request_id, "rating": "down", "comment": "x" * 41}
    response = client.post("/feedback", json=body)
    assert response.status_code == 422
    assert response.json() == {"detail": "Please keep your comment under 40 characters."}
    assert store.recent()[0].rating is None


def test_feedback_store_failure_is_a_friendly_503(client):
    app.dependency_overrides[get_chat_exchange_log] = BrokenLog
    response = client.post("/feedback", json={"request_id": "r1", "rating": "up"})
    assert response.status_code == 503
    assert response.json() == {"detail": FEEDBACK_UNAVAILABLE}
