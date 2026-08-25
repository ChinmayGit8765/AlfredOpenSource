---
tags: [standard, testing, llm]
status: enforced
applies-to: [alfred/domain/structured.py]
---

# Structured Output Contracts

## What it is

Every value that crosses from a language model into the program passes
through a schema. The model is asked for JSON matching that schema, the
reply is validated, and an invalid reply is either repaired through a
bounded retry or raised. No code path reads model prose and picks pieces
out of it.

## Why it matters here

A language model's output is untrusted input from a non-deterministic
source. That is the same threat category as a network payload, and it gets
the same treatment: parse, do not validate ad hoc.

The specific failure this prevents is not the obvious one. Obvious garbage
gets caught by any parser. The dangerous case is a **plausible partial
parse**: a regex that pulls the first `{...}` out of a reply, gets a
fragment, and produces a `Plan` with three items where the model wrote
seven. Nothing raises. The owner gets a week that is quietly missing half
of itself, and there is no error anywhere to notice.

The second reason is that model output reaches the tool dispatcher. A tool
call is a name and an argument dict. If those are extracted by string
handling rather than by a validated schema, the shape of what reaches the
security gate is whatever the model happened to emit.

## What good looks like

- One function is the only path to the model. Everything else calls it.
- The schema is a real model class (pydantic v2 here), not a dict of
  expected keys.
- **Bounded** repair: on a validation failure, send the errors back and ask
  again, at most a couple of times, then raise. Unbounded retry against a
  local model is an infinite loop with a fan noise.
- Extraction of the JSON object is done by structure (brace matching that
  respects strings and escapes), not by a regex, because a regex on nested
  JSON is wrong for exactly the inputs you care about.
- The failure raises a typed error the caller can act on, and the raw text
  stays out of the log because it can contain owner data.

## What bad looks like

- `json.loads(reply)` with a bare `except`, falling back to a default. The
  default is now the system's behaviour whenever the model has a bad day,
  and nobody knows how often that is.
- `re.search(r"\{.*\}", text, re.S)`, which greedily matches to the last
  brace in the reply and silently swallows trailing commentary.
- Different call sites each doing their own parsing, so the reliability of
  the system varies by which feature you use.

## How ALFRED does it

`alfred/domain/structured.py` exposes `structured_call(model, schema=...)`,
generic over the pydantic model. It builds the JSON schema, calls
`ModelPort.complete`, extracts the object by depth-counting brace matching
that tracks string state, validates, and on failure re-prompts with the
validation errors up to `max_attempts` before raising `StructuredCallError`.

It is the **only** caller of `ModelPort.complete` outside the adapter
layer, and that is a test, not a convention.

## Verification

`test_structured_output_goes_through_structured_call` in
`tests/test_architecture.py` asserts the chokepoint by parsing the AST of
every module. `tests/test_structured.py` covers extraction against nested
objects, embedded braces in strings, trailing prose, the repair retry, and
the eventual raise.

## Sources

- Lexi Lambda, *Parse, Don't Validate*.
- OWASP LLM Top 10, LLM05 Improper Output Handling.
