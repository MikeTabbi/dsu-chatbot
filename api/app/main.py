"""Backend API entry point."""

import functools
import logging
import time
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from api.app.claude_client import ClaudeClient, ClaudeError, ClaudeReply, get_claude_client
from api.app.config import Settings, settings
from api.app.exchange_log import (
    Exchange,
    ExchangeLog,
    NullExchangeLog,
    Outcome,
    Rating,
    UnknownRequestError,
    get_exchange_log,
    now,
)
from api.app.prompt import build_prompt, linked_sources, parse_citations, prompt_version
from api.app.rate_limit import DAY, MINUTE, Limit, RateLimiter, client_ip, get_rate_limiter
from api.app.redact import redact
from api.app.request_guard import RequestGuard
from api.app.retriever import Retriever, get_retriever
from ingestion.chunk import Chunk

logging.basicConfig(
    level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
log = logging.getLogger(__name__)

NO_CONTENT_ANSWER = (
    "I couldn't find anything about that in the DSU information I have. "
    "Try searching desu.edu, or contact the DSU office that handles your question."
)
UNAVAILABLE_ANSWER = (
    "Sorry, I can't answer right now. Please try again in a few minutes, or visit desu.edu."
)
UNKNOWN_REQUEST = "No answer with that request ID was found."
FEEDBACK_UNAVAILABLE = "Sorry, your feedback couldn't be saved. Please try again later."
TOO_FAST = "You're sending questions faster than I can answer. Please wait a minute and try again."
DAILY_LIMIT = (
    "You've reached today's limit for questions. Please try again tomorrow, or visit desu.edu."
)
FEEDBACK_TOO_FAST = "That's a lot of feedback at once. Please wait a minute and try again."
BUSY_ANSWER = "The DSU assistant is busy right now. Please try again later, or visit desu.edu."

app = FastAPI(title="DSU Chatbot API", version="0.1.0")

# Added before CORS so CORS wraps it: the widget can read a 413 or 415 like any other error.
app.add_middleware(
    RequestGuard,
    paths=["/chat", "/feedback"],
    max_bytes=lambda: _current_settings().max_request_bytes,
)
# Lets the widget on the listed sites call the API. No cookies, so no credentials.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
    expose_headers=["Retry-After"],
)


class ChatRequest(BaseModel):
    question: str


class Source(BaseModel):
    title: str
    heading_path: str
    url: str
    last_updated: str | None  # YYYY-MM-DD when the page reports a parseable date


class ChatResponse(BaseModel):
    answer: str
    sources: list[Source]  # the pages the answer cites, one per URL, best match first
    request_id: str


class FeedbackRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=64)
    rating: Rating
    comment: str | None = None


class FeedbackResponse(BaseModel):
    request_id: str
    rating: Rating


def get_settings() -> Settings:
    return settings


def _current_settings() -> Settings:
    """get_settings, or the test's override of it, for code outside FastAPI's dependencies."""
    return app.dependency_overrides.get(get_settings, get_settings)()


@functools.cache
def get_chat_retriever() -> Retriever:
    """Built once (the local retriever indexes every chunk); tests override this dependency."""
    return get_retriever(settings)


@functools.cache
def get_chat_client() -> ClaudeClient:
    """Built once; tests override this dependency."""
    return get_claude_client(settings)


@functools.cache
def get_chat_exchange_log() -> ExchangeLog:
    """Built once; tests override this dependency. A bad EXCHANGE_LOG setting keeps nothing
    rather than stopping /chat from answering."""
    try:
        return get_exchange_log(settings)
    except Exception as e:
        log.warning("exchange log unavailable, keeping nothing: %s", e)
        return NullExchangeLog()


@functools.cache
def get_chat_rate_limiter() -> RateLimiter:
    """Built once, so the counts last as long as the process; tests override this dependency."""
    return get_rate_limiter(settings)


class RateLimitedError(Exception):
    def __init__(self, detail: str, retry_after: int):
        self.detail = detail
        self.retry_after = retry_after
        self.request_id = uuid.uuid4().hex


@app.exception_handler(RateLimitedError)
def _rate_limited(request: Request, e: RateLimitedError) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"detail": e.detail, "request_id": e.request_id},
        headers={"Retry-After": str(e.retry_after)},
    )


def limit_chat(
    request: Request,
    config: Annotated[Settings, Depends(get_settings)],
    limiter: Annotated[RateLimiter, Depends(get_chat_rate_limiter)],
) -> None:
    """Per-client limits on /chat. Runs before the question is read, so bad requests count too."""
    limits = [
        Limit("chat_per_minute", config.rate_limit_chat_per_minute, MINUTE),
        Limit("chat_per_day", config.rate_limit_chat_per_day, DAY),
    ]
    _limit(request, config, limiter, "chat", limits)


def limit_feedback(
    request: Request,
    config: Annotated[Settings, Depends(get_settings)],
    limiter: Annotated[RateLimiter, Depends(get_chat_rate_limiter)],
) -> None:
    limits = [Limit("feedback_per_minute", config.rate_limit_feedback_per_minute, MINUTE)]
    _limit(request, config, limiter, "feedback", limits)


def _limit(
    request: Request, config: Settings, limiter: RateLimiter, endpoint: str, limits: list[Limit]
) -> None:
    blocked = limiter.hit(f"{endpoint}:{client_ip(request, config.trust_proxy)}", limits)
    if blocked is None:
        return
    if endpoint == "feedback":
        detail = FEEDBACK_TOO_FAST
    else:
        detail = DAILY_LIMIT if blocked.limit.seconds == DAY else TOO_FAST
    error = RateLimitedError(detail, blocked.retry_after)
    # Request ID and outcome only: never the question, and not the IP address.
    log.info(
        "%s request_id=%s outcome=rate_limited limit=%s retry_after=%d",
        endpoint,
        error.request_id,
        blocked.limit.name,
        blocked.retry_after,
    )
    raise error


@app.get("/health")
def health() -> dict[str, str]:
    """Simple check that the server is up."""
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse, dependencies=[Depends(limit_chat)])
def chat(
    request: ChatRequest,
    retriever: Annotated[Retriever, Depends(get_chat_retriever)],
    client: Annotated[ClaudeClient, Depends(get_chat_client)],
    config: Annotated[Settings, Depends(get_settings)],
    exchanges: Annotated[ExchangeLog, Depends(get_chat_exchange_log)],
    limiter: Annotated[RateLimiter, Depends(get_chat_rate_limiter)],
):
    """Answer one question from retrieved DSU content, with the sources the answer cites."""
    request_id = uuid.uuid4().hex
    start = time.perf_counter()
    question = request.question.strip()
    if not question:
        raise HTTPException(422, "Please enter a question.")
    if len(question) > config.chat_max_question_chars:
        raise HTTPException(
            422, f"Please keep your question under {config.chat_max_question_chars} characters."
        )

    chunks = [r.chunk for r in retriever.search(question, config.chat_top_k)]

    def record(outcome: Outcome, answer: str, sources=(), reply: ClaudeReply | None = None):
        _record(exchanges, request_id, start, question, answer, outcome, chunks, sources, reply)

    if not chunks:
        _log_request(request_id, start, chunks, "no_content")
        record("no_sources", NO_CONTENT_ANSWER)
        return ChatResponse(answer=NO_CONTENT_ANSWER, sources=[], request_id=request_id)

    # The daily budget counts Claude calls from every client, so credits are safe even when the
    # traffic comes from many addresses. Questions with no chunks never call Claude or count.
    budget = Limit("claude_daily_calls", config.claude_daily_call_budget, DAY)
    if blocked := limiter.hit("global:claude", [budget]):
        _log_request(request_id, start, chunks, "busy reason=daily_budget")
        return JSONResponse(
            status_code=503,
            content={"detail": BUSY_ANSWER, "request_id": request_id},
            headers={"Retry-After": str(blocked.retry_after)},
        )

    prompt = build_prompt(question, chunks)
    try:
        reply = client.complete(prompt.system, prompt.messages)
    except ClaudeError as e:
        _log_request(request_id, start, chunks, f"claude_error reason={e.reason}")
        record("error", UNAVAILABLE_ANSWER)
        return JSONResponse(
            status_code=503, content={"detail": UNAVAILABLE_ANSWER, "request_id": request_id}
        )

    answer = parse_citations(reply.text, len(chunks))
    # A page the answer links is a page it used, even if its <cited> line leaves it out (a decline
    # that still links DSU's tuition page). A desu.edu link in no source's URL or text (possibly
    # made up) is logged, never shown.
    linked = linked_sources(answer.text, chunks)
    for url in linked.unknown:
        log.warning(
            "answer links a page not among its sources request_id=%s url=%s", request_id, url
        )
    cited = sorted(set(answer.cited) | set(linked.cited))
    sources = _sources([chunks[n - 1] for n in cited])
    _log_request(request_id, start, chunks, f"answered cited={len(cited)}")
    record("answered", answer.text, sources, reply)
    return ChatResponse(answer=answer.text, sources=sources, request_id=request_id)


@app.post("/feedback", response_model=FeedbackResponse, dependencies=[Depends(limit_feedback)])
def feedback(
    request: FeedbackRequest,
    config: Annotated[Settings, Depends(get_settings)],
    exchanges: Annotated[ExchangeLog, Depends(get_chat_exchange_log)],
):
    """Thumbs up or down on one answer, by its request ID. Rating again replaces the rating."""
    comment = (request.comment or "").strip() or None
    if comment and len(comment) > config.feedback_max_comment_chars:
        raise HTTPException(
            422, f"Please keep your comment under {config.feedback_max_comment_chars} characters."
        )
    try:
        exchanges.set_feedback(request.request_id, request.rating, comment and redact(comment))
    except UnknownRequestError:
        raise HTTPException(404, UNKNOWN_REQUEST) from None
    except Exception as e:  # the store is down: say so, without its details
        log.warning("feedback not saved request_id=%s error=%s", request.request_id, _name(e))
        raise HTTPException(503, FEEDBACK_UNAVAILABLE) from None
    log.info("feedback request_id=%s rating=%s", request.request_id, request.rating)
    return FeedbackResponse(request_id=request.request_id, rating=request.rating)


def _sources(chunks: list[Chunk]) -> list[Source]:
    """One source per URL, in retrieval order; the best-matching cited chunk names the section."""
    by_url: dict[str, Source] = {}
    for c in chunks:
        if c.source_url not in by_url:
            by_url[c.source_url] = Source(
                title=c.title,
                heading_path=c.heading_path,
                url=c.source_url,
                last_updated=_date(c.modified_time),
            )
    return list(by_url.values())


def _date(modified_time: str | None) -> str | None:
    try:
        return datetime.fromisoformat(modified_time).date().isoformat() if modified_time else None
    except ValueError:
        return None


def _record(
    exchanges: ExchangeLog,
    request_id: str,
    start: float,
    question: str,
    answer: str,
    outcome: Outcome,
    chunks: list[Chunk],
    sources: list[Source],
    reply: ClaudeReply | None,
) -> None:
    """Store the exchange, redacted. A failing store is a warning: the student still gets the
    answer. The warning never includes the question or answer, which could hold personal details."""
    try:
        exchanges.add(
            Exchange(
                request_id=request_id,
                timestamp=now(),
                question=redact(question),
                answer=redact(answer),  # answers can repeat what the student typed
                outcome=outcome,
                prompt_version=prompt_version(),
                latency_ms=round((time.perf_counter() - start) * 1000),
                source_urls=[s.url for s in sources],
                chunk_ids=[c.chunk_id for c in chunks],
                model=reply.model if reply else None,
                input_tokens=reply.input_tokens if reply else None,
                output_tokens=reply.output_tokens if reply else None,
            )
        )
    except Exception as e:
        log.warning("exchange not logged request_id=%s error=%s", request_id, _name(e))


def _name(error: Exception) -> str:
    """The error's type only: a store's message could quote the values it was given."""
    return type(error).__name__


def _log_request(request_id: str, start: float, chunks: list[Chunk], outcome: str) -> None:
    # The question is never in the app log; the exchange log keeps a redacted copy.
    latency_ms = (time.perf_counter() - start) * 1000
    log.info(
        "chat request_id=%s chunks=%d latency_ms=%.0f outcome=%s",
        request_id,
        len(chunks),
        latency_ms,
        outcome,
    )
