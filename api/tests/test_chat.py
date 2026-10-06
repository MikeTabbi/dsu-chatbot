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
from api.app.retriever import LocalKeywordRetriever, ScoredChunk
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


class OnePageRetriever:
    def __init__(self, page):
        self.page = page

    def search(self, question, k=5):
        return [ScoredChunk(self.page, 1.0)]


class RecordingRetriever(LocalKeywordRetriever):
    def search(self, question, k=5):
        self.k = k
        return super().search(question, k)


ANSWER = "Tubman-Lawson and Warren-Franklin have carpet."
QUESTION = "Which dorms have carpeted rooms?"


@pytest.fixture
def fake():
    return FakeClaudeClient(answer=f"{ANSWER}\n\n<cited>1, 2, 3</cited>")


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


def retrieved_chunks(retriever):
    return [r.chunk for r in retriever.search(QUESTION, 3)]


def ask(client, fake, answer):
    fake.answer = answer
    response = client.post("/chat", json={"question": QUESTION})
    assert response.status_code == 200
    return response.json()


def test_answers_with_the_sources_the_answer_cites(client, fake, retriever):
    response = client.post("/chat", json={"question": QUESTION})
    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == ANSWER  # the <cited> marker is stripped
    assert len(body["request_id"]) == 32

    # k comes from the setting, and the prompt is exactly what build_prompt makes of the chunks
    assert retriever.k == 3
    retrieved = retrieved_chunks(retriever)
    [(system, messages)] = fake.calls
    expected = build_prompt(QUESTION, retrieved)
    assert (system, messages) == (expected.system, expected.messages)

    # all three chunks cited, from two pages: one source per URL, best match first, date only
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


def test_markers_are_stripped_from_the_answer(client, fake):
    body = ask(client, fake, "Tubman-Lawson<cited>1</cited> has carpet.\n<CITED> 2 </CITED>")
    assert body["answer"] == "Tubman-Lawson has carpet."
    assert "cited" not in body["answer"].lower()


def test_only_cited_sources_are_returned(client, fake, retriever):
    retrieved = retrieved_chunks(retriever)
    n = next(i for i, c in enumerate(retrieved, 1) if c.source_url == HOUSING + "/compare")
    body = ask(client, fake, f"{ANSWER}\n<cited>{n}</cited>")
    assert [s["url"] for s in body["sources"]] == [HOUSING + "/compare"]


def test_cited_chunk_names_the_section(client, fake, retriever):
    retrieved = retrieved_chunks(retriever)
    n = max(i for i, c in enumerate(retrieved, 1) if c.source_url == HOUSING)
    body = ask(client, fake, f"{ANSWER}\n<cited>{n}</cited>")
    assert body["sources"][0]["heading_path"] == retrieved[n - 1].heading_path


@pytest.mark.parametrize("bad", ["4", "0", "99", "x", "-1", "1.5"])
def test_invalid_citation_is_ignored_and_logged(client, fake, retriever, caplog, bad):
    retrieved = retrieved_chunks(retriever)
    with caplog.at_level(logging.WARNING, logger="api.app.prompt"):
        body = ask(client, fake, f"{ANSWER}\n<cited>1, {bad}</cited>")
    assert body["answer"] == ANSWER
    assert [s["url"] for s in body["sources"]] == [retrieved[0].source_url]
    assert f"ignoring citation {bad!r}" in caplog.text


def test_no_citations_gives_no_sources(client, fake):
    body = ask(client, fake, "Sorry, I can only help with DSU questions.\n<cited></cited>")
    assert body["answer"] == "Sorry, I can only help with DSU questions."
    assert body["sources"] == []


def test_missing_marker_gives_no_sources_and_a_warning(client, fake, caplog):
    with caplog.at_level(logging.WARNING, logger="api.app.prompt"):
        body = ask(client, fake, ANSWER)
    assert body["answer"] == ANSWER
    assert body["sources"] == []
    assert "no <cited> marker" in caplog.text


def test_a_source_the_answer_links_is_cited_even_if_left_off_the_marker(client, fake, caplog):
    answer = f"I can only help with DSU questions. Compare halls [here]({HOUSING}/compare)."
    with caplog.at_level(logging.WARNING, logger="api.app.main"):
        body = ask(client, fake, f"{answer}\n<cited></cited>")
    assert body["answer"] == answer
    assert [s["url"] for s in body["sources"]] == [HOUSING + "/compare"]
    assert "not among its sources" not in caplog.text


def test_linked_and_marked_sources_are_merged(client, fake, retriever):
    retrieved = retrieved_chunks(retriever)
    n = next(i for i, c in enumerate(retrieved, 1) if c.source_url == HOUSING)
    body = ask(client, fake, f"{ANSWER} See {HOUSING}/compare.\n<cited>{n}</cited>")
    assert [s["url"] for s in body["sources"]] == [HOUSING, HOUSING + "/compare"]


@pytest.mark.parametrize(
    "url",
    [
        "https://www.desu.edu/made-up-page",
        HOUSING + "/compare/rates",  # starts like a source, but isn't one exactly
        "https://my.desu.edu/portal",
    ],
)
def test_a_dsu_link_to_no_source_is_not_cited_and_is_logged(client, fake, caplog, url):
    with caplog.at_level(logging.WARNING, logger="api.app.main"):
        body = ask(client, fake, f"Try {url}.\n<cited></cited>")
    assert body["sources"] == []
    assert (
        f"answer links a page not among its sources request_id={body['request_id']} url={url}\n"
        in caplog.text + "\n"
    )


def test_only_a_dsu_link_in_neither_the_sources_nor_their_text_is_logged(client, fake, caplog):
    pdf = "https://www.desu.edu/files/housing-contract.pdf"
    page = chunk(0, "Housing & Dining > Contract", f"Carpeted rooms. Sign the [contract]({pdf}).")
    app.dependency_overrides[get_chat_retriever] = lambda: OnePageRetriever(page)
    made_up = "https://www.desu.edu/files/housing-rules.pdf"
    with caplog.at_level(logging.WARNING, logger="api.app.main"):
        body = ask(client, fake, f"Sign {pdf}. Rules: {made_up}.\n<cited></cited>")
    assert fake.calls and body["sources"] == []
    assert f"url={pdf}" not in caplog.text
    assert f"request_id={body['request_id']} url={made_up}" in caplog.text


def test_a_link_to_another_site_is_neither_cited_nor_logged(client, fake, caplog):
    with caplog.at_level(logging.WARNING, logger="api.app.main"):
        body = ask(client, fake, "See https://www.morgan.edu/tuition.\n<cited></cited>")
    assert body["sources"] == []
    assert "not among its sources" not in caplog.text


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
