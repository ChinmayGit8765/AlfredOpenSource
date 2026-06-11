"""OllamaModelAdapter: ModelPort over the official ollama package.

Maps ALFRED's port vocabulary onto ollama.AsyncClient.chat: ModelMessage
becomes a role/content dict, json_schema becomes the format parameter so
Ollama constrains decoding natively, and ModelOptions merge with the
configured defaults. ensure_model picks a usable local model up front so
a missing pull fails loudly at startup instead of mid-conversation.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

import ollama

from alfred.config import ModelConfig
from alfred.errors import AlfredError, ConfigError
from alfred.ports.model import ModelMessage, ModelOptions

logger = logging.getLogger(__name__)


def _matches(wanted: str, available: str) -> bool:
    """True when the available model satisfies the wanted name.

    Exact match, or the available name's base before ":" equals the wanted
    name (so wanted "qwen3" is satisfied by a pulled "qwen3:8b").
    """
    return wanted == available or available.split(":", 1)[0] == wanted


class OllamaModelAdapter:
    """ModelPort backed by a local Ollama server."""

    def __init__(self, config: ModelConfig, *, client: object | None = None) -> None:
        self._config = config
        # client injection keeps tests offline; production builds the real one.
        self._client: Any = client if client is not None else ollama.AsyncClient(host=config.host)

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
        ollama_options: dict[str, Any] = {"temperature": temperature}
        if opts.max_tokens is not None:
            ollama_options["num_predict"] = opts.max_tokens

        kwargs: dict[str, Any] = {
            "model": opts.model or self._config.name,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "options": ollama_options,
        }
        if json_schema is not None:
            kwargs["format"] = dict(json_schema)

        try:
            response = await self._client.chat(**kwargs)
        except ConnectionError as exc:
            raise AlfredError(
                f"could not reach Ollama at {self._config.host}; "
                f"is the Ollama server running? ({exc})"
            ) from exc
        return response.message.content or ""

    async def ensure_model(self) -> str:
        """Return a locally available model name, preferring config.name.

        Falls back through config.fallbacks in order; raises ConfigError
        listing what is available when nothing usable is pulled.
        """
        try:
            listing = await self._client.list()
        except ConnectionError as exc:
            raise AlfredError(
                f"could not reach Ollama at {self._config.host}; "
                f"is the Ollama server running? ({exc})"
            ) from exc

        available = [m.model for m in listing.models if m.model]
        if any(_matches(self._config.name, name) for name in available):
            return self._config.name
        for fallback in self._config.fallbacks:
            if any(_matches(fallback, name) for name in available):
                logger.warning(
                    "model %s not pulled locally; falling back to %s",
                    self._config.name,
                    fallback,
                )
                return fallback
        raise ConfigError(
            f"no usable model: wanted {self._config.name} "
            f"(fallbacks: {', '.join(self._config.fallbacks) or 'none'}), "
            f"but Ollama at {self._config.host} only has: "
            f"{', '.join(available) or 'nothing'}"
        )
