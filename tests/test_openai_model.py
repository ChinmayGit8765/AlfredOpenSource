"""Tests for the OpenAI-compatible model adapter: payload mapping, key
handling (from the environment, never leaked), the response_format
fallback, and ensure_model's reachability-first semantics."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from alfred.adapters.openai_model import OpenAiModelAdapter
from alfred.config import AlfredConfig, ModelConfig
from alfred.errors import AlfredError, ConfigError
from alfred.ports.model import ModelMessage, ModelOptions

KEY_ENV = "ALFRED_TEST_LLM_KEY"  # dedicated name so ambient env never bleeds in


def make_config(**overrides: Any) -> ModelConfig:
    defaults: dict[str, Any] = {
        "provider": "openai",
        "host": "http://api.example/v1",
        "name": "test-model",
        "fallbacks": ["backup-model"],
        "api_key_env": KEY_ENV,
    }
    defaults.update(overrides)
    return ModelConfig(**defaults)


class _Response:
    def __init__(self, status_code: int = 200, payload: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


def chat_response(content: str) -> _Response:
    return _Response(200, {"choices": [{"message": {"content": content}}]})


def models_response(*ids: str) -> _Response:
    return _Response(200, {"data": [{"id": i} for i in ids]})


class StubClient:
    """Records calls; shaped like the slice of httpx.AsyncClient we use."""

    def __init__(
        self,
        posts: list[_Response | Exception] | None = None,
        gets: list[_Response | Exception] | None = None,
    ) -> None:
        self.posts: list[tuple[str, dict[str, Any], dict[str, str]]] = []
        self.gets: list[tuple[str, dict[str, str]]] = []
        self._posts = list(posts or [])
        self._gets = list(gets or [])
        self.closed = False

    async def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> Any:
        self.posts.append((url, json, headers))
        outcome = self._posts.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def get(self, url: str, *, headers: dict[str, str]) -> Any:
        self.gets.append((url, headers))
        outcome = self._gets.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def aclose(self) -> None:
        self.closed = True


def messages() -> list[ModelMessage]:
    return [
        ModelMessage(role="system", content="be brief"),
        ModelMessage(role="user", content="hello"),
    ]


async def test_complete_maps_messages_options_and_extracts_content() -> None:
    client = StubClient(posts=[chat_response("hi there")])
    adapter = OpenAiModelAdapter(make_config(temperature=0.2), client=client)

    reply = await adapter.complete(
        messages(), options=ModelOptions(max_tokens=64)
    )

    assert reply == "hi there"
    url, payload, _ = client.posts[0]
    assert url == "/chat/completions"
    assert payload["model"] == "test-model"
    assert payload["temperature"] == 0.2
    assert payload["max_tokens"] == 64
    assert payload["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hello"},
    ]


async def test_api_key_header_comes_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(KEY_ENV, "sk-test-123")
    client = StubClient(posts=[chat_response("ok")])
    adapter = OpenAiModelAdapter(make_config(), client=client)

    await adapter.complete(messages())

    _, _, headers = client.posts[0]
    assert headers == {"Authorization": "Bearer sk-test-123"}


async def test_keyless_endpoint_sends_no_auth_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(KEY_ENV, raising=False)
    client = StubClient(posts=[chat_response("ok")])
    adapter = OpenAiModelAdapter(make_config(), client=client)

    await adapter.complete(messages())

    _, _, headers = client.posts[0]
    assert "Authorization" not in headers


async def test_json_schema_becomes_response_format() -> None:
    client = StubClient(posts=[chat_response("{}")])
    adapter = OpenAiModelAdapter(make_config(), client=client)
    schema = {"title": "AgentReply", "type": "object", "properties": {}}

    await adapter.complete(messages(), json_schema=schema)

    _, payload, _ = client.posts[0]
    fmt = payload["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["name"] == "AgentReply"
    assert fmt["json_schema"]["schema"] == schema


async def test_rejected_response_format_retries_without_it() -> None:
    # Some compatible servers 400 on response_format; the run must survive
    # because the structured-call loop validates plain text anyway.
    client = StubClient(
        posts=[_Response(400, text="response_format unsupported"), chat_response("{}")]
    )
    adapter = OpenAiModelAdapter(make_config(), client=client)

    reply = await adapter.complete(messages(), json_schema={"title": "X", "type": "object"})

    assert reply == "{}"
    assert "response_format" in client.posts[0][1]
    assert "response_format" not in client.posts[1][1]


async def test_auth_failure_is_readable_and_never_leaks_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(KEY_ENV, "sk-super-secret")
    client = StubClient(posts=[_Response(401, text="unauthorized")])
    adapter = OpenAiModelAdapter(make_config(), client=client)

    with pytest.raises(AlfredError) as excinfo:
        await adapter.complete(messages())

    text = str(excinfo.value)
    assert "sk-super-secret" not in text
    assert KEY_ENV in text  # points the owner at the env var, not the value


async def test_server_error_surfaces_status_and_detail() -> None:
    client = StubClient(posts=[_Response(500, text="upstream exploded")])
    adapter = OpenAiModelAdapter(make_config(), client=client)

    with pytest.raises(AlfredError, match="500"):
        await adapter.complete(messages())


async def test_unreachable_host_raises_readably() -> None:
    client = StubClient(posts=[httpx.ConnectError("connection refused")])
    adapter = OpenAiModelAdapter(make_config(), client=client)

    with pytest.raises(AlfredError, match="could not reach"):
        await adapter.complete(messages())


async def test_unexpected_response_shape_raises() -> None:
    client = StubClient(posts=[_Response(200, {"unexpected": True})])
    adapter = OpenAiModelAdapter(make_config(), client=client)

    with pytest.raises(AlfredError, match="unexpected response shape"):
        await adapter.complete(messages())


async def test_ensure_model_resolves_listed_name_and_complete_uses_it() -> None:
    client = StubClient(
        gets=[models_response("other", "test-model")],
        posts=[chat_response("ok")],
    )
    adapter = OpenAiModelAdapter(make_config(), client=client)

    assert await adapter.ensure_model() == "test-model"
    await adapter.complete(messages())
    assert client.posts[0][1]["model"] == "test-model"


async def test_ensure_model_falls_back_when_primary_unlisted() -> None:
    client = StubClient(gets=[models_response("backup-model")])
    adapter = OpenAiModelAdapter(make_config(), client=client)

    assert await adapter.ensure_model() == "backup-model"


async def test_ensure_model_trusts_config_when_listing_unavailable() -> None:
    # /models is optional in the compatible ecosystem; reachability is
    # enough and the configured name is used as-is.
    client = StubClient(gets=[_Response(404, text="not found")])
    adapter = OpenAiModelAdapter(make_config(), client=client)

    assert await adapter.ensure_model() == "test-model"


async def test_ensure_model_warns_but_proceeds_when_name_unlisted() -> None:
    client = StubClient(gets=[models_response("something-else")])
    adapter = OpenAiModelAdapter(make_config(), client=client)

    assert await adapter.ensure_model() == "test-model"


async def test_ensure_model_auth_failure_raises_config_error() -> None:
    client = StubClient(gets=[_Response(401, text="unauthorized")])
    adapter = OpenAiModelAdapter(make_config(), client=client)

    with pytest.raises(ConfigError, match=KEY_ENV):
        await adapter.ensure_model()


async def test_close_closes_the_client() -> None:
    client = StubClient()
    adapter = OpenAiModelAdapter(make_config(), client=client)

    await adapter.close()

    assert client.closed


def test_build_model_picks_the_adapter_by_provider() -> None:
    from alfred.adapters.ollama_model import OllamaModelAdapter
    from alfred.runtime.composition import build_model

    api = build_model(AlfredConfig(llm=make_config()))
    local = build_model(AlfredConfig())

    assert isinstance(api, OpenAiModelAdapter)
    assert isinstance(local, OllamaModelAdapter)


def test_config_defaults_stay_local_and_key_reads_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ModelConfig()
    assert config.provider == "ollama"
    assert config.api_key_env == "ALFRED_LLM_API_KEY"

    monkeypatch.setenv(KEY_ENV, "sk-abc")
    assert make_config().api_key() == "sk-abc"
    monkeypatch.delenv(KEY_ENV)
    assert make_config().api_key() == ""
