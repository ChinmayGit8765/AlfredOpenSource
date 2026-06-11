"""Tests for alfred.domain.structured: the validated-LLM-call core."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from alfred.domain.structured import extract_json, structured_call
from alfred.errors import StructuredCallError
from alfred.testing.fakes import FakeModel


class _Verdict(BaseModel):
    """Small private schema for exercising the call loop."""

    title: str
    score: int


_VALID = '{"title": "ok", "score": 5}'


# ---------------------------------------------------------------------------
# structured_call
# ---------------------------------------------------------------------------


async def test_happy_path_clean_json_returns_validated_instance():
    model = FakeModel([_VALID])

    result = await structured_call(model, schema=_Verdict, system="sys", user="usr")

    assert isinstance(result, _Verdict)
    assert result == _Verdict(title="ok", score=5)
    assert len(model.calls) == 1


async def test_fenced_json_is_extracted():
    model = FakeModel(['```json\n{"title": "fenced", "score": 1}\n```'])

    result = await structured_call(model, schema=_Verdict, system="sys", user="usr")

    assert result == _Verdict(title="fenced", score=1)
    assert len(model.calls) == 1


async def test_json_surrounded_by_chatter_is_extracted():
    model = FakeModel(
        [
            "Sure! Here is the structured output you asked for:\n"
            '{"title": "chatty", "score": 2}\n'
            "Let me know if you need anything else."
        ]
    )

    result = await structured_call(model, schema=_Verdict, system="sys", user="usr")

    assert result == _Verdict(title="chatty", score=2)
    assert len(model.calls) == 1


async def test_retry_loop_recovers_and_feeds_back_validation_errors():
    model = FakeModel(
        [
            "this is not json at all",
            '{"title": "wrong type", "score": "not-a-number"}',
            '{"title": "third time", "score": 3}',
        ]
    )

    result = await structured_call(model, schema=_Verdict, system="sys", user="usr")

    assert result == _Verdict(title="third time", score=3)
    assert len(model.calls) == 3

    # Second call must carry the failed reply plus a retry user message
    # containing the first attempt's error text.
    second_messages = model.calls[1]["messages"]
    assert second_messages[-2].role == "assistant"
    assert second_messages[-2].content == "this is not json at all"
    retry_one = second_messages[-1]
    assert retry_one.role == "user"
    assert "Validation errors" in retry_one.content
    assert "no JSON object found" in retry_one.content

    # Third call's retry message must carry the pydantic error from the
    # schema-invalid second attempt (wrong type for "score").
    retry_two = model.calls[2]["messages"][-1]
    assert retry_two.role == "user"
    assert "Validation errors" in retry_two.content
    assert "score" in retry_two.content


async def test_exhaustion_raises_structured_call_error():
    model = FakeModel(["still not json"])  # last entry repeats every attempt

    with pytest.raises(StructuredCallError) as excinfo:
        await structured_call(
            model, schema=_Verdict, system="sys", user="usr", max_attempts=3
        )

    assert excinfo.value.attempts == 3
    assert excinfo.value.last_error
    assert len(model.calls) == 3


async def test_schema_json_schema_is_passed_to_port():
    model = FakeModel([_VALID])

    await structured_call(model, schema=_Verdict, system="sys", user="usr")

    json_schema = model.calls[0]["json_schema"]
    assert json_schema is not None
    assert "title" in json_schema["properties"]
    assert "score" in json_schema["properties"]


# ---------------------------------------------------------------------------
# extract_json
# ---------------------------------------------------------------------------


def test_extract_json_strips_markdown_fences():
    text = '```json\n{"a": 1}\n```'
    assert extract_json(text) == '{"a": 1}'


def test_extract_json_handles_nested_braces():
    text = '{"a": {"b": {"c": 1}}}'
    assert extract_json(text) == text


def test_extract_json_ignores_braces_inside_string_values():
    text = 'note: {"a": "open { and close } inside"} trailing junk'
    assert extract_json(text) == '{"a": "open { and close } inside"}'


def test_extract_json_handles_escaped_quotes_in_strings():
    text = '{"a": "quote \\" then } brace"}'
    assert extract_json(text) == text


def test_extract_json_strips_leading_and_trailing_chatter():
    text = 'Of course! Here you go: {"a": 1} hope that helps.'
    assert extract_json(text) == '{"a": 1}'


def test_extract_json_no_object_raises_value_error():
    with pytest.raises(ValueError):
        extract_json("there is no json here")


def test_extract_json_unterminated_object_raises_value_error():
    with pytest.raises(ValueError):
        extract_json('chatter {"a": 1, "b": ')
