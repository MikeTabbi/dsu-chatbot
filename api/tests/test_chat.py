import logging

import pytest
from fastapi.testclient import TestClient

from api.app.claude_client import ClaudeError, FakeClaudeClient
from api.app.config import Settings
from api.app.main import (
    NO_CONTENT_ANSWER,
    UNAVAILABLE_ANSWER,
    app,
    get_chat_client,
    get_chat_retriever,
    get_settings,
)
from api.app.prompt import build_prompt
from api.app.retriever import LocalKeywordRetriever
from ingestion.chunk import Chunk

HOUSING = "https://www.desu.edu/student-life/housing-dining"


def chunk(n, heading_path, text, url=HOUSING, modified_time="2026-03-10T17:00:30-04:00"):
    return Chunk(
        chunk_id=f"c{n}",
        chunk_index=n,
        source_url=url,
        title="Housing & Dining",
        heading_path=heading_path,
        modified_time=modified_time,
        topic="housing",
        low_text=False,
        word_count=len(text.split()),
        text=text,
    )


CHUNKS = [
    chunk(0, "Housing & Dining > Tubman-Lawson Hall", "Tubman-Lawson Hall has carpeted rooms."),
    chunk(
        1, "Housing & Dining > Warren-Franklin Hall", "Warren-Franklin Hall has carpeted suites."
    ),
    chunk(
        2,
        "Housing Comparison Matrix",
        "Carpeted: Tubman-Lawson, Warren-Franklin.",
        url=HOUSING + "/compare",
        modified_time=None,
    ),
    chunk(3, "Dining > Meal Plans", "Pick a meal plan.", url=HOUSING + "/dining"),
    *(
        chunk(n, f"Admissions > Step {n}", f"Application step {n}.", url=f"{HOUSING}/{n}")
        for n in range(4, 10)
    ),
]


class FailingClient:
    def complete(self, system, messages):
        raise ClaudeError("timeout", "APITimeoutError at https://internal.example/v1")


class RecordingRetriever(LocalKeywordRetriever):
    def search(self, question, k=5):
        self.k = k
        return super().search(question, k)


@pytest.fixture
def fake():
    return FakeClaudeClient(answer="Tubman-Lawson and Warren-Franklin have carpet.")


@pytest.fixture
def retriever():
    return RecordingRetriever(CHUNKS)


@pytest.fixture
def client(fake, retriever):
    app.dependency_overrides[get_chat_retriever] = lambda: retriever
    app.dependency_overrides[get_chat_client] = lambda: fake
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, chat_top_k=3, chat_max_question_chars=50
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_answers_with_the_sources_claude_was_given(client, fake, retriever):
    response = client.post("/chat", json={"question": "Which dorms have carpeted rooms?"})
    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "Tubman-Lawson and Warren-Franklin have carpet."
    assert len(body["request_id"]) == 32

    # k comes from the setting, and the prompt is exactly what build_prompt makes of the chunks
    assert retriever.k == 3
    retrieved = [r.chunk for r in retriever.search("Which dorms have carpeted rooms?", 3)]
    [(system, messages)] = fake.calls
    expected = build_prompt("Which dorms have carpeted rooms?", retrieved)
    assert (system, messages) == (expected.system, expected.messages)

    # three chunks from two pages: one source per URL, best match first, date only
    assert body["sources"] == [
        {
            "title": "Housing & Dining",
            "heading_path": retrieved[0].heading_path,
            "url": HOUSING,
            "last_updated": "2026-03-10",
        },
        {
            "title": "Housing & Dining",
            "heading_path": "Housing Comparison Matrix",
            "url": HOUSING + "/compare",
            "last_updated": None,
        },
    ]


def test_question_is_trimmed(client, fake):
    response = client.post("/chat", json={"question": "  carpeted rooms?\n"})
    assert response.status_code == 200
    assert "<question>\ncarpeted rooms?\n</question>" in fake.calls[0][1][0]["content"]


@pytest.mark.parametrize("question", ["", "   ", "\n\t"])
def test_empty_question_is_rejected(client, fake, question):
    response = client.post("/chat", json={"question": question})
    assert response.status_code == 422
    assert response.json() == {"detail": "Please enter a question."}
    assert fake.calls == []


def test_too_long_question_is_rejected(client, fake):
    response = client.post("/chat", json={"question": "carpet " * 10})
    assert response.status_code == 422
    assert response.json() == {"detail": "Please keep your question under 50 characters."}
    assert fake.calls == []


def test_missing_question_is_rejected(client):
    assert client.post("/chat", json={}).status_code == 422


def test_zero_chunks_answers_no_content_without_calling_claude(client, fake):
    response = client.post("/chat", json={"question": "What's the wifi password?"})
    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == NO_CONTENT_ANSWER
    assert body["sources"] == []
    assert body["request_id"]
    assert fake.calls == []


def test_claude_error_returns_503_with_a_friendly_message(client):
    app.dependency_overrides[get_chat_client] = FailingClient
    response = client.post("/chat", json={"question": "Which dorms have carpeted rooms?"})
    assert response.status_code == 503
    body = response.json()
    assert body["detail"] == UNAVAILABLE_ANSWER
    assert len(body["request_id"]) == 32
    for internal in ("timeout", "APITimeoutError", "internal.example", "Traceback"):
        assert internal not in response.text


def test_logs_request_id_chunks_and_latency_but_not_the_question(client, caplog):
    with caplog.at_level(logging.INFO, logger="api.app.main"):
        body = client.post("/chat", json={"question": "Which dorms have carpeted rooms?"}).json()
    assert f"chat request_id={body['request_id']} chunks=3 latency_ms=" in caplog.text
    assert "outcome=answered" in caplog.text
    assert "carpeted rooms" not in caplog.text


def test_claude_error_reason_is_logged(client, caplog):
    app.dependency_overrides[get_chat_client] = FailingClient
    with caplog.at_level(logging.INFO, logger="api.app.main"):
        client.post("/chat", json={"question": "Which dorms have carpeted rooms?"})
    assert "outcome=claude_error reason=timeout" in caplog.text
