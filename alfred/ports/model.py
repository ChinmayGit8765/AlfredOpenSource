"""ModelPort: the brain socket.

Any LLM backend (Ollama today, anything else tomorrow) is hidden behind
this protocol. The domain never knows which engine is answering.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

Role = Literal["system", "user", "assistant", "tool"]


class ModelMessage(BaseModel):
    """One turn of a model conversation."""

    model_config = ConfigDict(frozen=True)

    role: Role
    content: str


class ModelOptions(BaseModel):
    """Per-call generation options. All fields optional; adapters apply defaults."""

    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None


@runtime_checkable
class ModelPort(Protocol):
    """A chat-completion capable language model."""

    async def complete(
        self,
        messages: Sequence[ModelMessage],
        *,
        json_schema: Mapping[str, Any] | None = None,
        options: ModelOptions | None = None,
    ) -> str:
        """Return the model's reply text for the given conversation.

        When json_schema is provided, adapters that support native
        constrained decoding (Ollama's format parameter) must use it;
        adapters that do not must still return their best-effort text.
        Validation and retry live in the domain, not here.
        """
        ...
