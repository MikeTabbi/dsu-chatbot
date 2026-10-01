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
from pydantic import BaseModel

from api.app.claude_client import ClaudeClient, ClaudeError, get_claude_client
from api.app.config import Settings, settings
from api.app.prompt import build_prompt
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

app = FastAPI(title="DSU Chatbot API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.allowed_origins.split(",") if o.strip()],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
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
    sources: list[Source]  # the pages Claude was given, one per URL, best match first
    request_id: str


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
):
    """Answer one question from retrieved DSU content, with the sources Claude was given."""
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
    if not chunks:
        _log_request(request_id, start, chunks, "no_content")
        return ChatResponse(answer=NO_CONTENT_ANSWER, sources=[], request_id=request_id)

    prompt = build_prompt(question, chunks)
    try:
        reply = client.complete(prompt.system, prompt.messages)
    except ClaudeError as e:
        _log_request(request_id, start, chunks, f"claude_error reason={e.reason}")
        return JSONResponse(
            status_code=503, content={"detail": UNAVAILABLE_ANSWER, "request_id": request_id}
        )

    _log_request(request_id, start, chunks, "answered")
    return ChatResponse(answer=reply.text, sources=_sources(chunks), request_id=request_id)


def _sources(chunks: list[Chunk]) -> list[Source]:
    """One source per URL, in retrieval order; the best-matching chunk names the section."""
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


def _log_request(request_id: str, start: float, chunks: list[Chunk], outcome: str) -> None:
    # The question text is not logged; logging exchanges for review is #8.
    latency_ms = (time.perf_counter() - start) * 1000
    log.info(
        "chat request_id=%s chunks=%d latency_ms=%.0f outcome=%s",
        request_id,
        len(chunks),
        latency_ms,
        outcome,
    )
