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

# httpx is already in the dependency tree via the ollama package itself,
# the same stance the telegram adapter takes.
import httpx
import ollama

from alfred.config import ModelConfig
from alfred.errors import AlfredError, ConfigError
from alfred.ports.model import ModelMessage, ModelOptions

logger = logging.getLogger(__name__)

# The ollama package defaults timeout to None (infinite httpx timeouts).
# The runtime serializes everything behind one handler lock, so a single
# hung generation (machine sleep, GPU wedge, LAN host dying mid-response)
# would deadlock the entire service, heartbeat and shutdown included.
# 300s read rather than something tighter: chat() is non-streaming, so the
# read timeout bounds cold model load plus the whole generation, and a
# large local model on weak hardware can legitimately need minutes.
_CONNECT_TIMEOUT_S = 8.0
_READ_TIMEOUT_S = 300.0


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
        self._client: Any = (
            client
            if client is not None
            else ollama.AsyncClient(
                host=config.host,
                timeout=httpx.Timeout(_READ_TIMEOUT_S, connect=_CONNECT_TIMEOUT_S),
            )
        )
        # Set by ensure_model: the exact locally-available name that satisfied
        # the configured one. Chatting with a bare name like "qwen3" makes
        # Ollama resolve it to ":latest", which 404s when only a tagged
        # variant ("qwen3:8b") is pulled.
        self._resolved: str | None = None

    def _timeout_error(self, exc: httpx.TimeoutException) -> AlfredError:
        # A read timeout does not prove the server is down: it may be up
        # with generation stalled, so the message must not suggest
        # "is the server running?". Name the budget that actually expired.
        budget = (
            _CONNECT_TIMEOUT_S if isinstance(exc, httpx.ConnectTimeout) else _READ_TIMEOUT_S
        )
        return AlfredError(
            f"timed out waiting for Ollama at {self._config.host} after "
            f"{budget:.0f}s; the model may be overloaded or the connection lost"
        )

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

        model = opts.model or self._resolved or self._config.name
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "options": ollama_options,
        }
        if json_schema is not None:
            kwargs["format"] = dict(json_schema)

        try:
            response = await self._client.chat(**kwargs)
        except httpx.TimeoutException as exc:
            raise self._timeout_error(exc) from exc
        except ConnectionError as exc:
            raise AlfredError(
                f"could not reach Ollama at {self._config.host}; "
                f"is the Ollama server running? ({exc})"
            ) from exc
        except ollama.ResponseError as exc:
            raise AlfredError(
                f"Ollama rejected the request for model '{model}': {exc}. "
                f"If the model is missing, pull it with: ollama pull {model}"
            ) from exc
        return response.message.content or ""

    async def ensure_model(self) -> str:
        """Resolve and remember a locally available model name.

        Prefers config.name, then config.fallbacks in order. Returns the
        exact available name that matched (a wanted "qwen3" satisfied by a
        pulled "qwen3:8b" resolves to "qwen3:8b"), and subsequent complete()
        calls default to it. Raises ConfigError listing what is available
        when nothing usable is pulled.
        """
        try:
            listing = await self._client.list()
        except httpx.TimeoutException as exc:
            raise self._timeout_error(exc) from exc
        except ConnectionError as exc:
            raise AlfredError(
                f"could not reach Ollama at {self._config.host}; "
                f"is the Ollama server running? ({exc})"
            ) from exc

        available: list[str] = [m.model for m in listing.models if m.model]
        for wanted in [self._config.name, *self._config.fallbacks]:
            resolved = next(
                (name for name in available if _matches(wanted, name)), None
            )
            if resolved is not None:
                if wanted != self._config.name:
                    logger.warning(
                        "model %s not pulled locally; falling back to %s",
                        self._config.name,
                        resolved,
                    )
                self._resolved = resolved
                return resolved
        raise ConfigError(
            f"no usable model: wanted {self._config.name} "
            f"(fallbacks: {', '.join(self._config.fallbacks) or 'none'}), "
            f"but Ollama at {self._config.host} only has: "
            f"{', '.join(available) or 'nothing'}"
        )
