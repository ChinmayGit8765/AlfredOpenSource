"""Validated LLM calls: the reliability core.

Every structured model output in ALFRED flows through structured_call.
The model is asked for JSON matching a pydantic schema; the reply is
parsed and validated; on failure the validation errors are fed back so
the model can correct itself, a bounded number of times. Raw LLM text is
never trusted as structured data anywhere else in the system.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, ValidationError

from alfred.errors import StructuredCallError
from alfred.ports.model import ModelMessage, ModelOptions, ModelPort

logger = logging.getLogger(__name__)

_RETRY_TEMPLATE = (
    "Your previous reply did not match the required JSON schema.\n"
    "Validation errors:\n{errors}\n\n"
    "Reply again with ONLY a single valid JSON object matching the schema. "
    "No prose, no markdown fences, no explanation."
)


def extract_json(text: str) -> str:
    """Best-effort extraction of the first JSON object in model text.

    Handles markdown fences, leading chatter, and trailing junk. Uses a
    string-aware brace walk rather than regex so braces inside JSON string
    values do not confuse the match. Raises ValueError when no complete
    object is present.
    """
    cleaned = text.strip()
    if "```" in cleaned:
        # Prefer the first fenced block that contains an object; models
        # often wrap JSON in ```json fences despite instructions.
        for i, part in enumerate(cleaned.split("```")):
            if i % 2 == 1 and "{" in part:
                cleaned = part.removeprefix("json").removeprefix("JSON")
                break
    start = cleaned.find("{")
    if start == -1:
        raise ValueError("no JSON object found in model output")
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(cleaned)):
        ch = cleaned[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return cleaned[start : i + 1]
    raise ValueError("unterminated JSON object in model output")


async def structured_call[T: BaseModel](
    model: ModelPort,
    *,
    schema: type[T],
    system: str,
    user: str,
    history: list[ModelMessage] | None = None,
    options: ModelOptions | None = None,
    max_attempts: int = 3,
) -> T:
    """Call the model and return a validated instance of schema.

    The schema's JSON Schema is passed to the port so capable backends can
    constrain decoding natively; extraction and validation here are the
    backstop that makes the result trustworthy regardless of backend.
    """
    json_schema = schema.model_json_schema()
    messages: list[ModelMessage] = [ModelMessage(role="system", content=system)]
    if history:
        messages.extend(history)
    messages.append(ModelMessage(role="user", content=user))

    last_error = ""
    for attempt in range(1, max_attempts + 1):
        raw = await model.complete(messages, json_schema=json_schema, options=options)
        try:
            return schema.model_validate_json(extract_json(raw))
        except (ValueError, ValidationError) as exc:
            last_error = str(exc)
            logger.warning(
                "structured output failed validation (attempt %d/%d, schema=%s): %s",
                attempt,
                max_attempts,
                schema.__name__,
                last_error,
            )
            messages.append(ModelMessage(role="assistant", content=raw))
            messages.append(
                ModelMessage(role="user", content=_RETRY_TEMPLATE.format(errors=last_error))
            )

    raise StructuredCallError(
        f"model output failed {schema.__name__} validation after {max_attempts} attempts",
        attempts=max_attempts,
        last_error=last_error,
    )
