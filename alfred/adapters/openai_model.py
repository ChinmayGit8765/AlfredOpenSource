"""OpenAiModelAdapter: ModelPort over any OpenAI-compatible chat API.

The /chat/completions dialect is spoken by OpenAI, OpenRouter, Groq,
Together, DeepSeek, LM Studio, vLLM, llama.cpp's server, and Ollama's own
/v1 endpoint, so this one adapter covers a hosted provider and a private
box in the next room alike. httpx is already in the dependency tree via
ollama, so API connectivity costs no new dependency.

The API key comes from the environment (config.api_key_env), never from a
config file, and never appears in logs or error text. A missing key is
allowed on purpose: keyless private endpoints are common, and a provider
that requires one refuses with a readable 401 surfaced to the owner.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from alfred.config import ModelConfig
from alfred.errors import AlfredError, ConfigError
from alfred.ports.model import ModelMessage, ModelOptions

logger = logging.getLogger(__name__)

# Big models on modest endpoints are slow; a generous read deadline beats
# aborting a plan mid-generation. Connect stays short so a dead host fails
# fast at startup.
_TIMEOUT = httpx.Timeout(120.0, connect=8.0)


def _schema_name(schema: Mapping[str, Any]) -> str:
    """A response_format-safe name; providers enforce ^[A-Za-z0-9_-]+$."""
    title = str(schema.get("title") or "output")
    return re.sub(r"[^A-Za-z0-9_-]", "_", title) or "output"


class OpenAiModelAdapter:
    """ModelPort backed by an OpenAI-compatible chat-completions endpoint."""

    def __init__(self, config: ModelConfig, *, client: Any | None = None) -> None:
        self._config = config
        # client injection keeps tests offline; production builds the real one.
        self._client: Any = (
            client
            if client is not None
            else httpx.AsyncClient(base_url=config.host.rstrip("/"), timeout=_TIMEOUT)
        )
        # Set by ensure_model when the provider's listing confirms a name.
        self._resolved: str | None = None

    def _headers(self) -> dict[str, str]:
        # Built per request, not baked into the client, so a key exported
        # after startup is picked up without a restart.
        key = self._config.api_key()
        return {"Authorization": f"Bearer {key}"} if key else {}

    async def complete(
        self,
        messages: Sequence[ModelMessage],
        *,
        json_schema: Mapping[str, Any] | None = None,
        options: ModelOptions | None = None,
    ) -> str:
        opts = options or ModelOptions()
        temperature = (
            opts.temperature if opts.temperature is not None else self._config.temperature
        )
        payload: dict[str, Any] = {
            "model": opts.model or self._resolved or self._config.name,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
        }
        if opts.max_tokens is not None:
            payload["max_tokens"] = opts.max_tokens
        if json_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": _schema_name(json_schema),
                    "schema": dict(json_schema),
                    "strict": False,
                },
            }

        response = await self._post_chat(payload)
        if response.status_code == 400 and "response_format" in payload:
            # Not every compatible server implements constrained decoding;
            # the structured-call retry loop validates plain text anyway,
            # so ask again unconstrained instead of failing the run. A copy,
            # not a pop: the original request must stay observable as sent.
            logger.debug(
                "endpoint rejected response_format (HTTP 400); retrying without it"
            )
            retry_payload = {
                k: v for k, v in payload.items() if k != "response_format"
            }
            response = await self._post_chat(retry_payload)
        self._raise_for_status(response, payload["model"])

        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise AlfredError(
                f"unexpected response shape from the model API at "
                f"{self._config.host}: {exc}"
            ) from exc
        return content or ""

    async def _post_chat(self, payload: dict[str, Any]) -> Any:
        try:
            return await self._client.post(
                "/chat/completions", json=payload, headers=self._headers()
            )
        except httpx.HTTPError as exc:
            raise AlfredError(
                f"could not reach the model API at {self._config.host}; "
                f"is the endpoint up? ({exc})"
            ) from exc

    def _raise_for_status(self, response: Any, model: str) -> None:
        if response.status_code in (401, 403):
            # Deliberately does not echo the key or any header.
            raise AlfredError(
                f"the model API at {self._config.host} rejected the request "
                f"(HTTP {response.status_code}); check the key exported in "
                f"{self._config.api_key_env}"
            )
        if response.status_code >= 400:
            detail = (response.text or "")[:200]
            raise AlfredError(
                f"the model API at {self._config.host} rejected the request for "
                f"model '{model}' (HTTP {response.status_code}): {detail}"
            )

    async def ensure_model(self) -> str:
        """Confirm the endpoint is reachable and resolve a model name.

        Mirrors OllamaModelAdapter.ensure_model: prefer config.name, then
        fallbacks, against the provider's /models listing. Providers that
        do not implement /models (or gate it) still pass on reachability
        alone, trusting the configured name; only auth failures and an
        unreachable host raise.
        """
        try:
            response = await self._client.get("/models", headers=self._headers())
        except httpx.HTTPError as exc:
            raise AlfredError(
                f"could not reach the model API at {self._config.host}; "
                f"is the endpoint up? ({exc})"
            ) from exc
        if response.status_code in (401, 403):
            raise ConfigError(
                f"the model API at {self._config.host} rejected the request "
                f"(HTTP {response.status_code}); check the key exported in "
                f"{self._config.api_key_env}"
            )

        available: list[str] = []
        if response.status_code < 400:
            try:
                data = response.json().get("data", [])
                available = [
                    str(item["id"]) for item in data if isinstance(item, dict) and "id" in item
                ]
            except (ValueError, TypeError, AttributeError):
                available = []

        if not available:
            # Reachable but no usable listing: trust the configured name and
            # let a bad one fail readably on the first completion.
            self._resolved = self._config.name
            return self._config.name

        for wanted in [self._config.name, *self._config.fallbacks]:
            if wanted in available:
                if wanted != self._config.name:
                    logger.warning(
                        "model %s not offered by the endpoint; falling back to %s",
                        self._config.name,
                        wanted,
                    )
                self._resolved = wanted
                return wanted

        # Listings lag behind aliases and dated snapshots, so an unlisted
        # name is a warning, never a hard stop.
        logger.warning(
            "model %s is not in the endpoint's listing (%d models offered); using it anyway",
            self._config.name,
            len(available),
        )
        self._resolved = self._config.name
        return self._config.name

    async def close(self) -> None:
        aclose = getattr(self._client, "aclose", None)
        if aclose is not None:
            await aclose()
