"""Offline tests for OllamaModelAdapter via an injected stub client.

The stub mirrors the shapes of ollama 0.6.2: chat() returns an object with
message.content (Optional[str]) and list() returns an object with .models,
each carrying a .model name, exactly as in ollama._types.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from alfred.adapters.ollama_model import OllamaModelAdapter
from alfred.config import ModelConfig
from alfred.errors import AlfredError, ConfigError
from alfred.ports.model import ModelMessage, ModelOptions


class _Message:
    def __init__(self, content: str | None) -> None:
        self.role = "assistant"
        self.content = content


class _ChatResponse:
    def __init__(self, content: str | None) -> None:
        self.message = _Message(content)


class _ListedModel:
    def __init__(self, model: str | None) -> None:
        self.model = model


class _ListResponse:
    def __init__(self, names: list[str | None]) -> None:
        self.models = [_ListedModel(n) for n in names]


class StubClient:
    """Records chat kwargs and serves canned chat/list responses."""

    def __init__(
        self,
        content: str | None = "hello",
        models: list[str | None] | None = None,
    ) -> None:
        self.content = content
        self.models = models or []
        self.chat_kwargs: dict[str, Any] | None = None

    async def chat(self, **kwargs: Any) -> _ChatResponse:
        self.chat_kwargs = kwargs
        return _ChatResponse(self.content)

    async def list(self) -> _ListResponse:
        return _ListResponse(self.models)


class FailingClient:
    """Simulates the ConnectionError ollama raises when the server is down."""

    async def chat(self, **kwargs: Any) -> Any:
        raise ConnectionError("Failed to connect to Ollama.")

    async def list(self) -> Any:
        raise ConnectionError("Failed to connect to Ollama.")


class TimeoutClient:
    """Simulates the raw httpx timeouts ollama lets escape unwrapped."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def chat(self, **kwargs: Any) -> Any:
        raise self._exc

    async def list(self) -> Any:
        raise self._exc


def make_adapter(
    client: object, **config_overrides: Any
) -> tuple[OllamaModelAdapter, ModelConfig]:
    config = ModelConfig(**config_overrides)
    return OllamaModelAdapter(config, client=client), config


MESSAGES = [
    ModelMessage(role="system", content="you are alfred"),
    ModelMessage(role="user", content="plan my week"),
]


async def test_complete_maps_messages_to_role_content_dicts() -> None:
    stub = StubClient(content="ok")
    adapter, config = make_adapter(stub)
    result = await adapter.complete(MESSAGES)
    assert result == "ok"
    assert stub.chat_kwargs is not None
    assert stub.chat_kwargs["messages"] == [
        {"role": "system", "content": "you are alfred"},
        {"role": "user", "content": "plan my week"},
    ]
    assert stub.chat_kwargs["model"] == config.name


async def test_complete_passes_json_schema_as_format() -> None:
    stub = StubClient()
    adapter, _ = make_adapter(stub)
    schema = {"type": "object", "properties": {"reply": {"type": "string"}}}
    await adapter.complete(MESSAGES, json_schema=schema)
    assert stub.chat_kwargs is not None
    assert stub.chat_kwargs["format"] == schema


async def test_complete_omits_format_without_schema() -> None:
    stub = StubClient()
    adapter, _ = make_adapter(stub)
    await adapter.complete(MESSAGES)
    assert stub.chat_kwargs is not None
    assert "format" not in stub.chat_kwargs


async def test_options_model_overrides_config_name() -> None:
    stub = StubClient()
    adapter, config = make_adapter(stub, name="qwen3:8b")
    await adapter.complete(MESSAGES, options=ModelOptions(model="llama3.1:8b"))
    assert stub.chat_kwargs is not None
    assert stub.chat_kwargs["model"] == "llama3.1:8b"
    assert config.name == "qwen3:8b"


async def test_temperature_defaults_to_config() -> None:
    stub = StubClient()
    adapter, config = make_adapter(stub, temperature=0.7)
    await adapter.complete(MESSAGES)
    assert stub.chat_kwargs is not None
    assert stub.chat_kwargs["options"]["temperature"] == config.temperature
    assert "num_predict" not in stub.chat_kwargs["options"]


async def test_temperature_and_max_tokens_overrides() -> None:
    stub = StubClient()
    adapter, _ = make_adapter(stub, temperature=0.7)
    await adapter.complete(MESSAGES, options=ModelOptions(temperature=0.0, max_tokens=128))
    assert stub.chat_kwargs is not None
    assert stub.chat_kwargs["options"]["temperature"] == 0.0
    assert stub.chat_kwargs["options"]["num_predict"] == 128


async def test_none_content_becomes_empty_string() -> None:
    adapter, _ = make_adapter(StubClient(content=None))
    assert await adapter.complete(MESSAGES) == ""


async def test_complete_wraps_connection_failure_with_host_hint() -> None:
    adapter, config = make_adapter(FailingClient())
    with pytest.raises(AlfredError) as excinfo:
        await adapter.complete(MESSAGES)
    assert config.host in str(excinfo.value)


async def test_ensure_model_exact_match_returns_config_name() -> None:
    adapter, _ = make_adapter(
        StubClient(models=["qwen3:8b", "llama3.1:8b"]), name="qwen3:8b"
    )
    assert await adapter.ensure_model() == "qwen3:8b"


async def test_ensure_model_prefix_match_resolves_to_available_name() -> None:
    # A bare configured name resolves to the exact pulled tag: chatting with
    # "qwen3" would make Ollama look for "qwen3:latest", which may not exist.
    adapter, _ = make_adapter(StubClient(models=["qwen3:8b"]), name="qwen3")
    assert await adapter.ensure_model() == "qwen3:8b"


async def test_complete_defaults_to_resolved_model_after_ensure() -> None:
    client = StubClient(models=["qwen3:8b"])
    adapter, _ = make_adapter(client, name="qwen3")
    await adapter.ensure_model()
    await adapter.complete(MESSAGES)
    assert client.chat_kwargs["model"] == "qwen3:8b"


async def test_ensure_model_falls_back_when_primary_missing() -> None:
    adapter, _ = make_adapter(
        StubClient(models=["llama3.1:8b"]),
        name="qwen3:8b",
        fallbacks=["qwen2.5:7b", "llama3.1:8b"],
    )
    assert await adapter.ensure_model() == "llama3.1:8b"


async def test_ensure_model_raises_config_error_listing_models() -> None:
    adapter, _ = make_adapter(
        StubClient(models=["mistral:7b"]),
        name="qwen3:8b",
        fallbacks=["qwen2.5:7b"],
    )
    with pytest.raises(ConfigError) as excinfo:
        await adapter.ensure_model()
    message = str(excinfo.value)
    assert "qwen3:8b" in message  # what we wanted
    assert "qwen2.5:7b" in message  # the fallbacks we tried
    assert "mistral:7b" in message  # what is actually available


async def test_ensure_model_ignores_nameless_entries() -> None:
    adapter, _ = make_adapter(
        StubClient(models=[None, "qwen3:8b"]), name="qwen3:8b"
    )
    assert await adapter.ensure_model() == "qwen3:8b"


def test_default_client_has_finite_timeouts() -> None:
    # No injected client: constructing ollama.AsyncClient opens no
    # connection, so this stays offline. The ollama package defaults
    # timeout to None (infinite), and with the runtime serialized behind
    # one handler lock a single hung generation would deadlock everything;
    # this pins the guard against a silent regression.
    adapter = OllamaModelAdapter(ModelConfig())
    timeout = adapter._client._client.timeout
    assert timeout.connect is not None
    assert timeout.read is not None


async def test_complete_wraps_read_timeout_as_alfred_error() -> None:
    adapter, config = make_adapter(TimeoutClient(httpx.ReadTimeout("slow")))
    with pytest.raises(AlfredError) as excinfo:
        await adapter.complete(MESSAGES)
    message = str(excinfo.value)
    assert "timed out" in message
    assert config.host in message


async def test_ensure_model_wraps_connect_timeout_as_alfred_error() -> None:
    adapter, config = make_adapter(TimeoutClient(httpx.ConnectTimeout("no route")))
    with pytest.raises(AlfredError) as excinfo:
        await adapter.ensure_model()
    message = str(excinfo.value)
    assert "timed out" in message
    assert config.host in message
