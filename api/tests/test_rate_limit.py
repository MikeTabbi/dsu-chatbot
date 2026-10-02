"""Per-client rate limits, the daily Claude budget, and the request size and JSON checks."""

import logging
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from api.app.claude_client import FakeClaudeClient
from api.app.config import Settings
from api.app.main import (
    BUSY_ANSWER,
    DAILY_LIMIT,
    FEEDBACK_TOO_FAST,
    TOO_FAST,
    app,
    get_chat_client,
    get_chat_exchange_log,
    get_chat_rate_limiter,
    get_chat_retriever,
    get_settings,
)
from api.app.rate_limit import (
    DAY,
    MINUTE,
    InMemoryRateLimiter,
    Limit,
    NoRateLimiter,
    get_rate_limiter,
)
from api.app.request_guard import NOT_JSON, TOO_LARGE
from api.app.retriever import LocalKeywordRetriever
from api.tests.test_chat import CHUNKS

QUESTION = "Which dorms have carpeted rooms?"
NO_CONTENT_QUESTION = "What's the wifi password?"
NOON = datetime(2026, 10, 2, 12, 0, 15, tzinfo=UTC).timestamp()  # 15 seconds into a minute


class FakeClock:
    def __init__(self, now: float = NOON):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def fake():
    return FakeClaudeClient(answer="Tubman-Lawson has carpet.\n\n<cited>1</cited>")


@pytest.fixture
def overrides(clock, fake):
    """Small limits so tests reach them quickly; a test can change any setting with configure()."""
    limiter = InMemoryRateLimiter(clock)
    values = dict(
        rate_limit_chat_per_minute=3,
        rate_limit_chat_per_day=5,
        rate_limit_feedback_per_minute=4,
        claude_daily_call_budget=100,
    )
    app.dependency_overrides[get_chat_retriever] = lambda: LocalKeywordRetriever(CHUNKS)
    app.dependency_overrides[get_chat_client] = lambda: fake
    app.dependency_overrides[get_chat_rate_limiter] = lambda: limiter
    app.dependency_overrides[get_chat_exchange_log] = NoLog
    app.dependency_overrides[get_settings] = lambda: Settings(**values)
    yield values
    app.dependency_overrides.clear()


@pytest.fixture
def client(overrides):
    return TestClient(app)


class NoLog:
    def add(self, exchange):
        pass

    def set_feedback(self, request_id, rating, comment):
        pass


def configure(overrides, **settings):
    overrides.update(settings)


def ask(client, question=QUESTION, **headers):
    return client.post("/chat", json={"question": question}, headers=headers)


# The limiter itself ----------------------------------------------------------------------------


def test_per_minute_limit_resets_on_the_next_minute(clock):
    limiter = InMemoryRateLimiter(clock)
    limits = [Limit("per_minute", 2, MINUTE)]
    assert limiter.hit("a", limits) is None
    assert limiter.hit("a", limits) is None
    blocked = limiter.hit("a", limits)
    assert blocked.limit.name == "per_minute"
    assert blocked.retry_after == 45  # 12:00:15 -> 12:01:00
    assert limiter.hit("b", limits) is None  # other clients are counted separately

    clock.advance(44.5)
    assert limiter.hit("a", limits).retry_after == 1  # rounded up, never 0
    clock.advance(0.5)
    assert limiter.hit("a", limits) is None


def test_per_day_limit_waits_until_midnight_utc(clock):
    limiter = InMemoryRateLimiter(clock)
    limits = [Limit("per_minute", 10, MINUTE), Limit("per_day", 2, DAY)]
    limiter.hit("a", limits)
    limiter.hit("a", limits)
    clock.advance(MINUTE)  # a new minute doesn't help
    blocked = limiter.hit("a", limits)
    assert blocked.limit.name == "per_day"
    assert blocked.retry_after == 12 * 60 * 60 - 15 - MINUTE  # until 2026-10-03 00:00:00 UTC

    clock.advance(blocked.retry_after)
    assert limiter.hit("a", limits) is None


def test_blocked_requests_are_not_counted(clock):
    limiter = InMemoryRateLimiter(clock)
    limits = [Limit("per_minute", 1, MINUTE), Limit("per_day", 2, DAY)]
    limiter.hit("a", limits)
    for _ in range(5):  # refused by the minute limit, so they don't use up the day
        assert limiter.hit("a", limits).limit.name == "per_minute"
    clock.advance(MINUTE)
    assert limiter.hit("a", limits) is None


def test_zero_turns_a_limit_off(clock):
    limiter = InMemoryRateLimiter(clock)
    assert all(limiter.hit("a", [Limit("off", 0, MINUTE)]) is None for _ in range(50))


def test_old_counters_are_dropped(clock):
    limiter = InMemoryRateLimiter(clock)
    for n in range(100):
        limiter.hit(f"client-{n}", [Limit("per_minute", 5, MINUTE)])
    clock.advance(2 * MINUTE)
    limiter.hit("new", [Limit("per_minute", 5, MINUTE)])
    assert len(limiter._counts) == 1


def test_rate_limiter_setting_picks_the_implementation():
    assert isinstance(get_rate_limiter(Settings(rate_limiter="memory")), InMemoryRateLimiter)
    assert isinstance(get_rate_limiter(Settings(rate_limiter="none")), NoRateLimiter)
    with pytest.raises(ValueError, match="Unknown RATE_LIMITER"):
        get_rate_limiter(Settings(rate_limiter="redis"))


# /chat and /feedback ---------------------------------------------------------------------------


def test_chat_per_minute_limit_returns_429_with_retry_after(client, fake, clock):
    for _ in range(3):
        assert ask(client).status_code == 200
    response = ask(client)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "45"
    body = response.json()
    assert body["detail"] == TOO_FAST
    assert len(body["request_id"]) == 32
    assert len(fake.calls) == 3  # the limited request never reached Claude

    clock.advance(45)
    assert ask(client).status_code == 200


def test_chat_per_day_limit(client, clock):
    for _ in range(5):
        assert ask(client).status_code == 200
        clock.advance(MINUTE)
    response = ask(client)
    assert response.status_code == 429
    assert response.json()["detail"] == DAILY_LIMIT
    assert int(response.headers["retry-after"]) > 11 * 60 * 60


def test_invalid_requests_count_toward_the_limit(client):
    for _ in range(3):
        assert client.post("/chat", json={}).status_code == 422
    assert ask(client).status_code == 429


def test_feedback_has_its_own_looser_limit(client):
    for _ in range(3):
        ask(client)
    assert ask(client).status_code == 429  # /chat is used up; /feedback isn't
    body = {"request_id": "r1", "rating": "up"}
    for _ in range(4):
        assert client.post("/feedback", json=body).status_code == 200
    response = client.post("/feedback", json=body)
    assert response.status_code == 429
    assert response.json()["detail"] == FEEDBACK_TOO_FAST
    assert response.headers["retry-after"] == "45"


def test_health_is_never_limited(client, overrides):
    configure(overrides, rate_limit_chat_per_minute=1, rate_limit_feedback_per_minute=1)
    ask(client)
    assert ask(client).status_code == 429
    for _ in range(50):
        assert client.get("/health").status_code == 200


def test_limits_are_logged_with_request_id_and_outcome_only(client, caplog):
    for _ in range(3):
        ask(client)
    with caplog.at_level(logging.INFO, logger="api.app.main"):
        request_id = ask(client).json()["request_id"]
    assert (
        f"chat request_id={request_id} outcome=rate_limited limit=chat_per_minute retry_after=45"
        in caplog.text
    )
    assert "carpeted" not in caplog.text
    assert "testclient" not in caplog.text  # nor the client's address


# The daily Claude budget -----------------------------------------------------------------------


def test_daily_budget_stops_claude_calls_from_every_client(client, fake, overrides, caplog):
    configure(overrides, claude_daily_call_budget=2, trust_proxy=True)
    assert ask(client, **{"X-Forwarded-For": "203.0.113.1"}).status_code == 200
    assert ask(client, **{"X-Forwarded-For": "203.0.113.2"}).status_code == 200
    with caplog.at_level(logging.INFO, logger="api.app.main"):
        response = ask(client, **{"X-Forwarded-For": "203.0.113.3"})  # a new client, same budget
    assert response.status_code == 503
    body = response.json()
    assert body["detail"] == BUSY_ANSWER
    assert len(body["request_id"]) == 32
    assert int(response.headers["retry-after"]) == 12 * 60 * 60 - 15  # until midnight UTC
    assert len(fake.calls) == 2
    assert f"request_id={body['request_id']}" in caplog.text
    assert "outcome=busy reason=daily_budget" in caplog.text
    assert "carpeted" not in caplog.text


def test_questions_without_sources_dont_use_the_budget(client, fake, overrides):
    configure(overrides, claude_daily_call_budget=1)
    ask(client, NO_CONTENT_QUESTION)
    ask(client, NO_CONTENT_QUESTION)
    assert ask(client).status_code == 200
    assert len(fake.calls) == 1


def test_budget_resets_at_midnight_utc(client, fake, clock, overrides):
    configure(overrides, claude_daily_call_budget=1)
    assert ask(client).status_code == 200
    assert ask(client).status_code == 503
    clock.advance(DAY)
    assert ask(client).status_code == 200


# Who the client is -----------------------------------------------------------------------------


def test_forwarded_for_is_ignored_unless_the_proxy_is_trusted(client):
    for n in range(3):
        ask(client, **{"X-Forwarded-For": f"198.51.100.{n}"})
    # Changing the header doesn't make a new client: it's all the same connection.
    assert ask(client, **{"X-Forwarded-For": "198.51.100.99"}).status_code == 429


def test_trusted_proxy_counts_each_forwarded_address(client, overrides):
    configure(overrides, trust_proxy=True)
    for _ in range(3):
        assert ask(client, **{"X-Forwarded-For": "198.51.100.1"}).status_code == 200
    assert ask(client, **{"X-Forwarded-For": "198.51.100.1"}).status_code == 429
    assert ask(client, **{"X-Forwarded-For": "198.51.100.2"}).status_code == 200


@pytest.mark.parametrize(
    "spoofed",
    [
        "1.1.1.1, 198.51.100.1",  # a made-up first entry; the proxy added the last one
        "198.51.100.1:51234",  # Azure can include the port
        "  198.51.100.1  ",
    ],
)
def test_trusted_proxy_uses_the_address_the_proxy_added(client, overrides, spoofed):
    configure(overrides, trust_proxy=True)
    for _ in range(3):
        ask(client, **{"X-Forwarded-For": "198.51.100.1"})
    assert ask(client, **{"X-Forwarded-For": spoofed}).status_code == 429


def test_ipv6_clients_are_counted_per_64_block(client, overrides):
    configure(overrides, trust_proxy=True)
    for n in range(3):
        ask(client, **{"X-Forwarded-For": f"[2001:db8:1:2::{n}]:443"})
    assert ask(client, **{"X-Forwarded-For": "2001:db8:1:2:ffff::1"}).status_code == 429
    assert ask(client, **{"X-Forwarded-For": "2001:db8:1:3::1"}).status_code == 200


# Request size and type -------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/chat", "/feedback"])
def test_oversized_body_is_rejected(client, fake, overrides, path):
    configure(overrides, max_request_bytes=100)
    response = client.post(path, json={"question": "carpet " * 20, "request_id": "r"})
    assert response.status_code == 413
    assert response.json() == {"detail": TOO_LARGE}
    assert fake.calls == []


def test_oversized_body_without_a_length_is_rejected(client, fake, overrides):
    configure(overrides, max_request_bytes=100)

    def chunks():  # sent chunked, with no Content-Length header
        yield b'{"question": "'
        yield b"carpet " * 20
        yield b'"}'

    response = client.post("/chat", content=chunks(), headers={"Content-Type": "application/json"})
    assert response.status_code == 413
    assert fake.calls == []


def test_body_at_the_cap_is_accepted(client, overrides):
    body = b'{"question": "Which dorms have carpeted rooms?"}'
    configure(overrides, max_request_bytes=len(body))
    response = client.post("/chat", content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 200


@pytest.mark.parametrize("path", ["/chat", "/feedback"])
@pytest.mark.parametrize("content_type", [None, "text/plain", "application/x-www-form-urlencoded"])
def test_non_json_body_is_rejected(client, fake, path, content_type):
    headers = {"Content-Type": content_type} if content_type else {}
    response = client.post(path, content=b'{"question": "carpet"}', headers=headers)
    assert response.status_code == 415
    assert response.json() == {"detail": NOT_JSON}
    assert fake.calls == []


def test_json_with_a_charset_is_accepted(client):
    response = client.post(
        "/chat",
        content=b'{"question": "Which dorms have carpeted rooms?"}',
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    assert response.status_code == 200


def test_browser_can_read_retry_after():
    response = TestClient(app).get("/health", headers={"Origin": "http://localhost:8080"})
    assert "retry-after" in response.headers["access-control-expose-headers"].lower()
