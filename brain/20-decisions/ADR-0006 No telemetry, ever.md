---
tags: [adr]
status: accepted
date: 2026-08-25
---

# ADR 0006: No telemetry, ever

## Status

Accepted. This one is intended to be permanent.

## Context

Every maturing project eventually proposes anonymous usage statistics. The
arguments are good ones: which agents are used, where the builder loses
people, which model backends people actually run, how often lapse diagnosis
fires. That data would genuinely improve the product.

It is nonetheless incompatible with what this project is.

`docs/SPEC.md` states the thesis: an AI that "runs on hardware they own,
holds their data, and answers to one loyalty only: their flourishing". A
process that reports to its author is a process with a second loyalty,
however small and however anonymised.

The data is also not anonymisable in any meaningful sense. Agent names are
owner-authored and describe their life. Timing patterns are a behavioural
fingerprint. Even "how many agents are in the lapsing state" is a fact
about a person's month that they did not choose to publish.

And there is a structural argument that outlives any intention: a telemetry
channel is an outbound connection with a payload assembled from local
state. Once it exists, every future feature can widen it a little, and each
widening is individually reasonable.

## Decision

ALFRED does not send anything anywhere except:

1. the model backend the owner configured, and
2. the transports the owner configured.

No usage statistics, no crash reporting, no update checks, no
"anonymised" anything. Not opt-out. Not opt-in either, because an opt-in
telemetry channel is still a telemetry channel that ships in the binary.

## Consequences

### What this buys

The strongest form of the local-first claim, and one that can be verified
by reading rather than trusted: the domain cannot import an HTTP library,
and the adapters that can are wired to exactly two destinations.

It also removes an entire category of future work: no consent flow, no
privacy policy, no data retention question, no regulatory surface.

### What this costs

Development is blind. Priorities come from the maintainer's own use and
from what people report, which is a biased and thin sample. Some features
will be built that nobody wants, and some bugs will live longer than they
would with crash reports.

That cost is accepted explicitly rather than reluctantly.

### What we gave up

Product analytics, permanently.

## Alternatives considered

**Opt-in telemetry, off by default.** The usual compromise. Rejected: the
code path exists either way, and "off by default" is one config regression
away from on. It also weakens the claim from "cannot" to "does not
currently", which is a much less useful thing to tell someone deciding
whether to trust this with their health notes.

**A local-only stats command** the owner can run and choose to paste into
an issue. Not rejected. This keeps the useful part, gives the owner the
data about themselves, and moves the transmission decision to a human. Not
built, but it would not violate this ADR.

## Verification

Structural. `test_domain_imports_stay_inside_the_allowlist` means no domain
module can open a socket. The adapter layer is small enough to audit by
reading, and `pyproject.toml` lists five runtime dependencies, none of which
is an analytics client.

Should a stronger check ever be wanted: a test asserting that the set of
network-capable adapters is exactly the known list.
