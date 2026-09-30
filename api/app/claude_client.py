"""Send a system prompt and messages to Claude. /chat depends on the ClaudeClient interface only."""

import argparse
import logging
import time
from dataclasses import dataclass
from typing import Literal, Protocol, TypedDict

import anthropic

from api.app.config import Settings, settings

log = logging.getLogger(__name__)

# The SDK retries connection errors, timeouts, 408, 409, 429, and 5xx with backoff. One retry keeps
# the worst case near 2 x CLAUDE_TIMEOUT_SECONDS, so a visitor isn't left waiting on a stuck call.
MAX_RETRIES = 1


class ChatMessage(TypedDict):
    role: Literal["user", "assistant"]
    content: str


@dataclass
class ClaudeReply:
    text: str
    model: str  # the model that answered, as reported by the provider
    input_tokens: int
    output_tokens: int


class ClaudeError(Exception):
    """Any failure to get an answer. /chat catches this one error and shows a friendly message.

    reason is one of: timeout, rate_limit, connection, api_error, no_answer.
    """

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


class ClaudeClient(Protocol):
    def complete(self, system: str, messages: list[ChatMessage]) -> ClaudeReply:
        """Claude's answer to the conversation. Raises ClaudeError on any failure."""
        ...


class AnthropicClaudeClient:
    """Claude through Anthropic's API, using the official anthropic SDK."""

    def __init__(
        self,
        api_key: str,
        model: str,
        max_tokens: int,
        timeout: float,
        sdk_client: anthropic.Anthropic | None = None,  # tests pass a stub
    ):
        if sdk_client is None and not api_key:
            raise ValueError("ANTHROPIC_API_KEY is not set")
        self.model = model
        self.max_tokens = max_tokens
        # The key is passed explicitly so it only ever comes from ANTHROPIC_API_KEY (or .env).
        self._sdk = sdk_client or anthropic.Anthropic(
            api_key=api_key, timeout=timeout, max_retries=MAX_RETRIES
        )

    def complete(self, system: str, messages: list[ChatMessage]) -> ClaudeReply:
        start = time.perf_counter()
        try:
            response = self._sdk.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=messages,
            )
        except anthropic.APITimeoutError as e:  # before APIConnectionError, its parent class
            raise self._fail(start, "timeout", e) from e
        except anthropic.RateLimitError as e:
            raise self._fail(start, "rate_limit", e) from e
        except anthropic.APIConnectionError as e:
            raise self._fail(start, "connection", e) from e
        except anthropic.APIStatusError as e:
            raise self._fail(start, "api_error", e, f"HTTP {e.status_code}") from e
        except anthropic.APIError as e:
            raise self._fail(start, "api_error", e) from e

        latency_ms = (time.perf_counter() - start) * 1000
        reply = ClaudeReply(
            text="".join(b.text for b in response.content if b.type == "text").strip(),
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        log.info(
            "claude call model=%s input_tokens=%d output_tokens=%d latency_ms=%.0f stop_reason=%s",
            reply.model,
            reply.input_tokens,
            reply.output_tokens,
            latency_ms,
            response.stop_reason,
        )
        if response.stop_reason == "refusal" or not reply.text:
            raise ClaudeError("no_answer", f"stop_reason={response.stop_reason}")
        if response.stop_reason == "max_tokens":
            log.warning("claude answer cut off at CLAUDE_MAX_OUTPUT_TOKENS=%d", self.max_tokens)
        return reply

    def _fail(self, start: float, reason: str, error: Exception, detail: str = "") -> ClaudeError:
        latency_ms = (time.perf_counter() - start) * 1000
        detail = detail or type(error).__name__
        log.warning(
            "claude call failed model=%s reason=%s detail=%s latency_ms=%.0f",
            self.model,
            reason,
            detail,
            latency_ms,
        )
        return ClaudeError(reason, detail)


class FakeClaudeClient:
    """A predictable answer with no network call or API key, for tests and local development."""

    model = "fake"

    def __init__(self, answer: str | None = None):
        self.answer = answer  # None: echo the last user message
        self.calls: list[tuple[str, list[ChatMessage]]] = []

    def complete(self, system: str, messages: list[ChatMessage]) -> ClaudeReply:
        self.calls.append((system, messages))
        question = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        text = self.answer if self.answer is not None else f"[fake answer] {question}"
        input_words = len(system.split()) + sum(len(m["content"].split()) for m in messages)
        reply = ClaudeReply(text, self.model, input_words, len(text.split()))
        log.info(
            "claude call model=%s input_tokens=%d output_tokens=%d latency_ms=0",
            reply.model,
            reply.input_tokens,
            reply.output_tokens,
        )
        return reply


def get_claude_client(config: Settings = settings) -> ClaudeClient:
    """The client the CLAUDE_CLIENT setting selects."""
    if config.claude_client == "anthropic":
        return AnthropicClaudeClient(
            api_key=config.anthropic_api_key.get_secret_value(),
            model=config.claude_model,
            max_tokens=config.claude_max_output_tokens,
            timeout=config.claude_timeout_seconds,
        )
    if config.claude_client == "fake":
        return FakeClaudeClient()
    if config.claude_client == "azure":
        raise NotImplementedError("Claude through Azure is not built yet (#19)")
    raise ValueError(
        f"Unknown CLAUDE_CLIENT {config.claude_client!r}: expected 'anthropic', 'fake', or 'azure'"
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Ask Claude one question and print the reply.")
    parser.add_argument("question")
    parser.add_argument("--system", default="Answer in one short sentence.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    try:
        reply = get_claude_client().complete(
            args.system, [{"role": "user", "content": args.question}]
        )
    except ClaudeError as e:
        raise SystemExit(f"Claude call failed: {e}") from e
    print(reply.text)
    print(
        f"model={reply.model} input_tokens={reply.input_tokens} output_tokens={reply.output_tokens}"
    )


if __name__ == "__main__":
    main()
