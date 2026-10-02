"""Backend API entry point."""

import functools
import logging
import time
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
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
from api.app.prompt import build_prompt, parse_citations, prompt_version
from api.app.redact import redact
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

app = FastAPI(title="DSU Chatbot API", version="0.1.0")

# Lets the widget on the listed sites call the API. No cookies, so no credentials.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
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


@app.get("/health")
def health() -> dict[str, str]:
    """Simple check that the server is up."""
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
def chat(
    request: ChatRequest,
    retriever: Annotated[Retriever, Depends(get_chat_retriever)],
    client: Annotated[ClaudeClient, Depends(get_chat_client)],
    config: Annotated[Settings, Depends(get_settings)],
    exchanges: Annotated[ExchangeLog, Depends(get_chat_exchange_log)],
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
    sources = _sources([chunks[n - 1] for n in answer.cited])
    _log_request(request_id, start, chunks, f"answered cited={len(answer.cited)}")
    record("answered", answer.text, sources, reply)
    return ChatResponse(answer=answer.text, sources=sources, request_id=request_id)


@app.post("/feedback", response_model=FeedbackResponse)
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
