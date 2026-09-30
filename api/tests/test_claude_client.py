import logging
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from api.app.claude_client import (
    MAX_RETRIES,
    AnthropicClaudeClient,
    ClaudeError,
    FakeClaudeClient,
    get_claude_client,
    main,
)
from api.app.config import Settings

KEY = "sk-ant-test-not-a-real-key"
SYSTEM = "You answer questions about Delaware State University."
MESSAGES = [{"role": "user", "content": "When does housing open?"}]
REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def response(text="Move-in starts August 20.", stop_reason="end_turn", thinking=False):
    content = [SimpleNamespace(type="text", text=text)] if text is not None else []
    if thinking:
        content.insert(0, SimpleNamespace(type="thinking", thinking=""))
    return SimpleNamespace(
        content=content,
        model="claude-opus-5",
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=42, output_tokens=7),
    )


class StubSDK:
    """Stands in for anthropic.Anthropic: records the request, returns or raises a set result."""

    def __init__(self, result):
        self.result = result
        self.requests = []
        self.messages = self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def client_for(result):
    stub = StubSDK(result)
    return AnthropicClaudeClient(KEY, "claude-opus-5", 512, 5.0, sdk_client=stub), stub


def status_error(cls, code):
    return cls("error", response=httpx2.Response(code, request=REQUEST), body=None)


def test_returns_answer_text_and_usage():
    client, stub = client_for(response(thinking=True))
    reply = client.complete(SYSTEM, MESSAGES)
    assert reply.text == "Move-in starts August 20."
    assert (reply.model, reply.input_tokens, reply.output_tokens) == ("claude-opus-5", 42, 7)
    [request] = stub.requests
    assert request == {
        "model": "claude-opus-5",
        "max_tokens": 512,
        "system": SYSTEM,
        "messages": MESSAGES,
    }


def test_logs_model_tokens_and_latency_but_never_the_key(caplog):
    client, _ = client_for(response())
    with caplog.at_level(logging.INFO, logger="api.app.claude_client"):
        client.complete(SYSTEM, MESSAGES)
    assert "model=claude-opus-5 input_tokens=42 output_tokens=7 latency_ms=" in caplog.text
    assert KEY not in caplog.text


@pytest.mark.parametrize(
    "error, reason",
    [
        (anthropic.APITimeoutError(request=REQUEST), "timeout"),
        (status_error(anthropic.RateLimitError, 429), "rate_limit"),
        (anthropic.APIConnectionError(request=REQUEST), "connection"),
        (status_error(anthropic.InternalServerError, 529), "api_error"),
        (status_error(anthropic.AuthenticationError, 401), "api_error"),
        (status_error(anthropic.BadRequestError, 400), "api_error"),
    ],
)
def test_sdk_failures_become_one_app_error(error, reason, caplog):
    client, _ = client_for(error)
    with pytest.raises(ClaudeError) as raised:
        client.complete(SYSTEM, MESSAGES)
    assert raised.value.reason == reason
    assert raised.value.__cause__ is error
    assert f"reason={reason}" in caplog.text and KEY not in caplog.text


@pytest.mark.parametrize("result", [response(stop_reason="refusal", text=None), response(text="")])
def test_refusal_or_empty_answer_is_an_error(result):
    client, _ = client_for(result)
    with pytest.raises(ClaudeError) as raised:
        client.complete(SYSTEM, MESSAGES)
    assert raised.value.reason == "no_answer"


def test_cut_off_answer_is_returned_with_a_warning(caplog):
    client, _ = client_for(response(stop_reason="max_tokens"))
    assert client.complete(SYSTEM, MESSAGES).text
    assert "cut off" in caplog.text


def test_sdk_client_gets_key_timeout_and_limited_retries():
    client = AnthropicClaudeClient(KEY, "claude-opus-5", 512, 5.0)
    assert client._sdk.api_key == KEY
    assert client._sdk.timeout == 5.0
    assert client._sdk.max_retries == MAX_RETRIES <= 2


def test_missing_key_fails_fast():
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        get_claude_client(Settings(claude_client="anthropic", anthropic_api_key=""))


def test_blank_env_values_keep_the_defaults(monkeypatch):
    monkeypatch.setenv("CLAUDE_MODEL", "")
    monkeypatch.setenv("CLAUDE_CLIENT", "")
    config = Settings(_env_file=None)
    assert (config.claude_model, config.claude_client) == ("claude-opus-5", "fake")


def test_key_is_hidden_when_settings_are_printed():
    config = Settings(anthropic_api_key=KEY)
    assert KEY not in repr(config) and KEY not in str(config.model_dump())


def test_fake_client_is_predictable_and_records_calls():
    fake = FakeClaudeClient()
    reply = fake.complete(SYSTEM, MESSAGES)
    assert reply.text == "[fake answer] When does housing open?"
    assert reply.model == "fake" and reply.input_tokens > 0 and reply.output_tokens > 0
    assert fake.calls == [(SYSTEM, MESSAGES)]
    assert FakeClaudeClient("Canned.").complete(SYSTEM, MESSAGES).text == "Canned."


def test_client_is_selected_by_config():
    assert isinstance(get_claude_client(Settings(claude_client="fake")), FakeClaudeClient)
    client = get_claude_client(
        Settings(
            claude_client="anthropic",
            anthropic_api_key=KEY,
            claude_model="claude-sonnet-5",
            claude_max_output_tokens=256,
            claude_timeout_seconds=12,
        )
    )
    assert isinstance(client, AnthropicClaudeClient)
    assert (client.model, client.max_tokens, client._sdk.timeout) == ("claude-sonnet-5", 256, 12)

    with pytest.raises(NotImplementedError):
        get_claude_client(Settings(claude_client="azure"))
    with pytest.raises(ValueError):
        get_claude_client(Settings(claude_client="openai"))


def test_cli_prints_answer_model_and_tokens(monkeypatch, capsys):
    monkeypatch.setattr("api.app.claude_client.get_claude_client", lambda: FakeClaudeClient())
    main(["Where is the library?"])
    out = capsys.readouterr().out
    assert "[fake answer] Where is the library?" in out
    assert "model=fake input_tokens=" in out


def test_cli_exits_with_a_message_on_failure(monkeypatch):
    client, _ = client_for(anthropic.APITimeoutError(request=REQUEST))
    monkeypatch.setattr("api.app.claude_client.get_claude_client", lambda: client)
    with pytest.raises(SystemExit, match="Claude call failed: timeout"):
        main(["Where is the library?"])
