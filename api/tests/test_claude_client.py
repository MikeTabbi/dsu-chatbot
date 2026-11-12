import logging
from datetime import date
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
from api.app.config import settings as app_settings
from api.app.prompt import build_prompt, parse_citations
from api.tests.test_prompt import chunk

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


def test_a_json_schema_is_sent_as_structured_output_format():
    stub = StubSDK(response())
    schema = {"type": "object", "properties": {"ids": {"type": "array"}}, "required": ["ids"]}
    client = AnthropicClaudeClient(
        KEY, "claude-haiku", 200, 5.0, sdk_client=stub, json_schema=schema
    )
    client.complete(SYSTEM, MESSAGES)
    [request] = stub.requests
    assert request["output_config"] == {"format": {"type": "json_schema", "schema": schema}}


def test_thinking_and_effort_are_sent_when_set():
    stub = StubSDK(response())
    client = AnthropicClaudeClient(
        KEY, "claude-sonnet-5-5", 512, 5.0, sdk_client=stub, thinking="between_tools", effort="low"
    )
    client.complete(SYSTEM, MESSAGES)
    [request] = stub.requests
    assert request["thinking"] == {"type": "between_tools"}
    assert request["output_config"] == {"effort": "low"}


def test_effort_is_sent_beside_a_json_schema():
    stub = StubSDK(response())
    schema = {"type": "object"}
    client = AnthropicClaudeClient(
        KEY, "claude-sonnet-5-5", 512, 5.0, sdk_client=stub, json_schema=schema, effort="medium"
    )
    client.complete(SYSTEM, MESSAGES)
    [request] = stub.requests
    assert request["output_config"] == {
        "format": {"type": "json_schema", "schema": schema},
        "effort": "medium",
    }
    assert "thinking" not in request


def test_thinking_and_effort_come_from_settings():
    config = Settings(
        _env_file=None,
        claude_client="anthropic",
        anthropic_api_key=KEY,
        claude_thinking="adaptive",
        claude_effort="medium",
    )
    client = get_claude_client(config)
    assert (client.thinking, client.effort) == ("adaptive", "medium")
    default = get_claude_client(
        Settings(_env_file=None, claude_client="anthropic", anthropic_api_key=KEY)
    )
    assert (default.thinking, default.effort) == ("", "")


@pytest.mark.parametrize(
    "setting",
    [{"claude_thinking": "off"}, {"claude_thinking": "enabled"}, {"claude_effort": "min"}],
)
def test_unknown_thinking_or_effort_is_refused(setting):
    with pytest.raises(ValueError):
        Settings(_env_file=None, **setting)


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


def test_tests_ignore_dot_env_and_use_the_fake_client(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(f"CLAUDE_CLIENT=anthropic\nANTHROPIC_API_KEY={KEY}\n")
    monkeypatch.chdir(tmp_path)  # Settings reads .env from the working directory
    for config in (Settings(), app_settings):  # new settings, and the ones the app imported
        assert config.claude_client == "fake"
        assert config.anthropic_api_key.get_secret_value() == ""
    assert isinstance(get_claude_client(), FakeClaudeClient)


def test_key_is_hidden_when_settings_are_printed():
    config = Settings(anthropic_api_key=KEY)
    assert KEY not in repr(config) and KEY not in str(config.model_dump())


def test_fake_client_is_predictable_and_records_calls():
    fake = FakeClaudeClient()
    reply = fake.complete(SYSTEM, MESSAGES)
    assert reply.text == "[fake answer] When does housing open?\n\n<cited></cited>"
    assert reply.model == "fake" and reply.input_tokens > 0 and reply.output_tokens > 0
    assert fake.calls == [(SYSTEM, MESSAGES)]
    assert FakeClaudeClient("Canned.").complete(SYSTEM, MESSAGES).text == "Canned."


def test_fake_client_cites_source_1_only_when_the_prompt_has_sources():
    with_sources = build_prompt("Which dorms?", [chunk()], today=date(2026, 10, 1))
    reply = FakeClaudeClient().complete(with_sources.system, with_sources.messages)
    assert reply.text.endswith("\n\n<cited>1</cited>")
    assert parse_citations(reply.text, 1).cited == [1]

    no_sources = build_prompt("Which dorms?", [], today=date(2026, 10, 1))
    reply = FakeClaudeClient().complete(no_sources.system, no_sources.messages)
    assert reply.text.endswith("\n\n<cited></cited>")
    assert parse_citations(reply.text, 0).cited == []


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


# stream() ------------------------------------------------------------------------------------


class StubStream:
    """Stands in for the SDK's MessageStream: the text pieces, then the final message. `fail`
    is raised after `fail_after` pieces, as the SDK raises an error that arrives mid-stream."""

    def __init__(self, pieces, final, fail=None, fail_after=0):
        self.pieces, self.final, self.fail, self.fail_after = pieces, final, fail, fail_after
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    @property
    def text_stream(self):
        for n, piece in enumerate(self.pieces):
            if self.fail and n == self.fail_after:
                raise self.fail
            yield piece
        if self.fail and self.fail_after >= len(self.pieces):
            raise self.fail

    def get_final_message(self):
        return self.final


class StreamingStubSDK(StubSDK):
    def __init__(self, stream=None, error=None):
        super().__init__(None)
        self.stub_stream, self.error = stream, error

    def stream(self, **kwargs):
        self.requests.append(kwargs)
        if self.error:
            raise self.error
        return self.stub_stream


def streaming_client(stub, **kwargs):
    return AnthropicClaudeClient(KEY, "claude-opus-5", 512, 5.0, sdk_client=stub, **kwargs)


def test_stream_sends_the_text_pieces_then_the_reply(caplog):
    text = "Move-in starts August 20."
    stub = StreamingStubSDK(StubStream(["Move-in ", "starts ", "August 20."], response(text)))
    client = streaming_client(stub, thinking="adaptive", effort="low")
    with caplog.at_level(logging.INFO, logger="api.app.claude_client"):
        items = list(client.stream(SYSTEM, MESSAGES))
    assert items[:-1] == ["Move-in ", "starts ", "August 20."]
    assert items[-1] == client._reply(0, response(text))
    [request] = stub.requests
    assert request["thinking"] == {"type": "adaptive"}  # the same request as complete()
    assert request["output_config"] == {"effort": "low"}
    assert stub.stub_stream.closed
    assert "output_tokens=7" in caplog.text


@pytest.mark.parametrize(
    "error, reason",
    [
        (anthropic.APITimeoutError(request=REQUEST), "timeout"),
        (anthropic.APIConnectionError(request=REQUEST), "connection"),
        (status_error(anthropic.InternalServerError, 529), "api_error"),
    ],
)
@pytest.mark.parametrize("fail_after", [0, 2])
def test_stream_errors_become_claude_errors_before_or_during_the_text(error, reason, fail_after):
    stub = StreamingStubSDK(StubStream(["a", "b", "c"], response(), error, fail_after))
    received = []
    with pytest.raises(ClaudeError) as e:
        for item in streaming_client(stub).stream(SYSTEM, MESSAGES):
            received.append(item)
    assert e.value.reason == reason
    assert received == ["a", "b"][:fail_after]
    assert stub.stub_stream.closed


def test_stream_error_opening_the_call_is_a_claude_error():
    stub = StreamingStubSDK(error=status_error(anthropic.RateLimitError, 429))
    with pytest.raises(ClaudeError) as e:
        list(streaming_client(stub).stream(SYSTEM, MESSAGES))
    assert e.value.reason == "rate_limit"


def test_stream_refusal_is_no_answer_after_the_text():
    stub = StreamingStubSDK(StubStream(["I can't"], response("I can't", stop_reason="refusal")))
    with pytest.raises(ClaudeError) as e:
        list(streaming_client(stub).stream(SYSTEM, MESSAGES))
    assert e.value.reason == "no_answer"


def test_closing_the_stream_early_closes_the_call():
    stub = StreamingStubSDK(StubStream(["a", "b", "c"], response()))
    items = streaming_client(stub).stream(SYSTEM, MESSAGES)
    assert next(items) == "a"
    items.close()  # the student left
    assert stub.stub_stream.closed


def test_fake_stream_sends_the_complete_answer_in_pieces():
    fake = FakeClaudeClient(answer="Tubman has carpet.\n<cited>1</cited>", chunk_chars=5)
    items = list(fake.stream(SYSTEM, MESSAGES))
    assert all(len(p) <= 5 for p in items[:-1])
    assert "".join(items[:-1]) == items[-1].text == fake.complete(SYSTEM, MESSAGES).text
