---
tags: [standard, people]
status: enforced
applies-to: [alfred/runtime/ui.py, alfred/runtime/cli.py]
---

# Accessibility and Terminal UX

## What it is

The terminal is a product surface with real constraints: encodings that
cannot represent a glyph, colour that some users cannot distinguish, and
screen readers that read whatever is printed.

## Why it matters here

The chat REPL is how most owners will meet ALFRED, and `chat --fake` is the
first thing a new contributor runs. If it crashes on their machine, that is
the whole impression.

The concrete hazard is encoding. A legacy Windows console encodes to a
codepage like cp1252 on write, and a glyph the codepage lacks is a
`UnicodeEncodeError`, not a substitution. So a decorative block character
in a banner is a crash on first run for a slice of users, in the exact
place where nothing has gone wrong yet.

## What good looks like

- **Probe the console, do not assume.** Try encoding a sample of the
  glyphs you intend to use, and fall back to an ASCII twin if it fails.
  Everything decorative has a plain equivalent.
- Colour is never the only signal. A status that is red carries a word too,
  because red and green are the most common confusion and a screen reader
  announces neither.
- All output through one module, so it can be probed, themed, redirected,
  and tested. No stray `print` from a library path.
- Errors as sentences, not tracebacks.
- Output that stays legible when piped to a file or read aloud.

## What bad looks like

- Unicode box drawing with no fallback.
- `print()` scattered through the codebase, which makes the output
  impossible to capture in a test or suppress in a pipe.
- A progress spinner that emits thousands of lines when not a TTY.
- Information carried only by colour.

## How ALFRED does it

`runtime/ui.py` defines `_can_encode()`, which attempts to encode a sample
of the glyphs it wants against the console's actual encoding and returns
False on `UnicodeEncodeError` or `LookupError`. `FANCY` gates every
decorative choice: the banner has a `_BANNER_FANCY` and a `_BANNER_PLAIN`
of identical shape, and the bullet is `·` or `-` accordingly. Its docstring
states the reason: on those consoles a missing glyph is a crash, not a
substitution.

All terminal output goes through `runtime/cli.py` and `runtime/ui.py`, a
rule stated in `CLAUDE.md`. Check lines pair a status word with the colour.

## Verification

`tests/test_ui.py` covers the fallback path. ruff's `T20` rules ban `print`
outside `scripts/`, so the single-output-module rule cannot erode quietly.
The CI matrix includes Windows, so an encoding regression fails there.

Open gap: nothing asserts the fancy and plain banners have the same number
of rows. They do, and the `zip(..., strict=True)` added alongside this note
turns a future mismatch into a crash rather than a silently truncated
masthead, which is the next best thing.

## Sources

- The `rich` documentation on console detection and non-TTY behaviour.
- WCAG 1.4.1 (use of colour), applied by analogy to a terminal.
