# Choosing and connecting a model

ALFRED's brain is swappable. Every model sits behind one port
(`ModelPort`), every structured request goes through one validated call
(`domain/structured.structured_call`), and the adapter is chosen in one
place (`llm.provider` in `config/alfred.yaml`). This guide covers what
works, what to pick for your hardware, and the exact config for every
common way of running a model.

The one-line answer to "does my model work": if it is an instruct or chat
tuned LLM that Ollama or any OpenAI-compatible server can run, yes. That
covers essentially every known downloadable free model and every major
hosted provider.

## Why almost any chat model works

ALFRED never trusts a model to freestyle. Every structured request:

1. sends the JSON schema of the expected reply with the request, so
   backends that support constrained decoding (Ollama `format`, the
   OpenAI-dialect `response_format`) are steered to emit valid JSON;
2. extracts and validates the reply against the pydantic schema
   (`extract_json` strips markdown fences and leading chatter first);
3. on a validation failure, feeds the errors back and retries, up to 3
   attempts, so a model that fumbles once gets to correct itself.

This loop is why a 7B free model is genuinely usable: the schema
discipline comes from the harness, not from model obedience. What the
model contributes is judgment; bigger models plan better, but small ones
still produce valid, working output.

Three real requirements:

| Requirement | Why |
|---|---|
| Instruct/chat tuned | Base (non-instruct) checkpoints ramble and ignore the output contract. Pick anything tagged instruct/chat; every model on ollama.com/library qualifies. |
| Text in, text out | Embedding, reranker, vision-only, TTS, and diffusion models are not chat models and do not apply. Multimodal chat models (e.g. vision-capable LLMs) work fine; ALFRED just uses their text side. |
| Roughly 8k+ context | Agent briefs carry the prompt, user model, memories, peer plans, and tool specs. Almost everything current clears this easily. |

## Path 1: local and free with Ollama (the default)

The default posture: the brain never leaves your machine, costs nothing,
and needs no key. Install [Ollama](https://ollama.com), pull a model, done.

```yaml
llm:
  provider: ollama            # the default; shown for clarity
  host: "http://127.0.0.1:11434"
  name: "qwen3:8b"
  fallbacks: ["qwen2.5:7b", "llama3.1:8b"]
```

`ensure_model` resolves the name against what is actually pulled and
walks `fallbacks` in order, so a missing pull fails loudly at startup
with a list of what is available, never mid-conversation.

### Known free model families (all work)

Everything in the Ollama library works; these are the families worth
knowing. Sizes are the commonly pulled quantized variants.

| Family | Sizes | Notes for ALFRED |
|---|---|---|
| Qwen 3 | 0.6B to 32B+ | The default (`qwen3:8b`). Strong JSON discipline and tool-style output for its size. |
| Qwen 2.5 | 0.5B to 72B | First fallback (`qwen2.5:7b`). Reliable, widely benchmarked. |
| Llama 3.1 / 3.2 / 3.3 | 1B to 70B | Second fallback (`llama3.1:8b`). 3.2 1B/3B run on very small machines. |
| Mistral / Ministral / Mistral Small | 3B to 24B | `mistral:7b` is a classic light option; Small 24B is a strong mid-size. |
| Gemma 3 | 1B to 27B | Good quality per GB; the 4B is a solid low-RAM pick. |
| Phi-4 | 14B | Strong reasoning for its size; MIT licensed. |
| DeepSeek-R1 distills | 1.5B to 70B | Reasoning models; see the note on thinking models below. |

Pull any of them the same way: `ollama pull <name>`, set `llm.name`, run
`alfred doctor`.

### Sizing to your hardware

Rule of thumb for the default 4-bit quantizations: a model needs roughly
0.6 to 0.7 GB of RAM (or VRAM) per billion parameters, plus headroom.

| Your machine | Pick | Expect |
|---|---|---|
| 8 GB RAM, no GPU | `llama3.2:3b`, `qwen3:4b`, `gemma3:4b` | Fast, decent plans; keep agent prompts tight. |
| 16 GB RAM or 8 GB VRAM | `qwen3:8b` (default), `llama3.1:8b` | The sweet spot; everything in this repo is tuned around this class. |
| 32 GB RAM or 16 GB VRAM | `qwen3:14b`, `phi4:14b`, `mistral-small` | Noticeably better judgment in plans and reflections. |
| 24 GB+ VRAM | `qwen3:32b`, `llama3.3:70b` (48 GB+) | Diminishing returns for ALFRED's workloads, but the reflections get sharp. |

Smaller than 3B works mechanically (the schema loop holds) but plan
quality drops: expect blunt plans and occasional retry churn.

### A note on thinking/reasoning models

DeepSeek-R1 style models emit a reasoning block before the answer.
Constrained decoding plus `extract_json`'s chatter-stripping absorb this,
but they respond slower and burn tokens on thinking, which a weekly
planning run does not need. Prefer a plain instruct model unless you
specifically want the reasoning quality.

## Path 2: local and free without Ollama

Anything that serves the OpenAI dialect locally is one config block away.
Same downloadable models, different runner:

```yaml
llm:
  provider: openai
  host: "http://127.0.0.1:1234/v1"   # your server's base URL
  name: "the-model-id-the-server-shows"
```

No key is needed for local servers; `ALFRED_LLM_API_KEY` can stay unset.

| Server | Default base URL | Notes |
|---|---|---|
| LM Studio | `http://127.0.0.1:1234/v1` | Point-and-click model downloads; enable the local server in settings. |
| llama.cpp (`llama-server`) | `http://127.0.0.1:8080/v1` | Runs raw GGUF files; grammar-enforced JSON supported. |
| vLLM | `http://127.0.0.1:8000/v1` | Production-grade throughput on a GPU box. |
| Jan | `http://127.0.0.1:1337/v1` | Desktop app with a bundled server. |
| KoboldCpp | `http://127.0.0.1:5001/v1` | GGUF runner with an OpenAI-compatible endpoint. |
| text-generation-webui | `http://127.0.0.1:5000/v1` | Enable the openai extension. |
| Ollama on another machine | `http://<host>:11434/v1` | Ollama speaks the OpenAI dialect at `/v1`; use this to borrow a beefier box on your LAN. |

Servers that reject `response_format` with the usual HTTP 400 are handled
automatically: ALFRED retries the request unconstrained and the
validation loop takes over.

## Path 3: hosted APIs (opt-in, needs a key)

When you want a bigger brain than your hardware carries. Set the key in
the environment, never in config:

```yaml
llm:
  provider: openai
  host: "https://openrouter.ai/api/v1"
  name: "the-provider's-model-id"
```

```powershell
$env:ALFRED_LLM_API_KEY = "your-key"
alfred doctor
```

| Provider | Base URL | Notes |
|---|---|---|
| OpenRouter | `https://openrouter.ai/api/v1` | One key, one URL, hundreds of models from every lab (including free-tier routes). The easiest single answer. |
| OpenAI | `https://api.openai.com/v1` | GPT models. |
| Anthropic | `https://api.anthropic.com/v1` | Claude models via Anthropic's OpenAI-compatible endpoint. |
| Groq | `https://api.groq.com/openai/v1` | Open models (Llama, Qwen...) at very high speed; generous free tier. |
| Together | `https://api.together.xyz/v1` | Large open-model catalog. |
| DeepSeek | `https://api.deepseek.com/v1` | DeepSeek chat and reasoner. |
| Mistral | `https://api.mistral.ai/v1` | Mistral's hosted models; free tier available. |
| Google | `https://generativelanguage.googleapis.com/v1beta/openai` | Gemini via Google's OpenAI-compatible endpoint. |
| xAI | `https://api.x.ai/v1` | Grok models. |

Model ids change as providers ship; check the provider's model list and
put the exact id in `llm.name`. `alfred doctor` probes the endpoint; a
name the provider does not list logs a warning, and a truly wrong id
fails with a readable 404 on first use.

Known exception: Azure OpenAI authenticates with different headers and a
deployment-based URL scheme, so it does not work out of the box; front it
with a compatibility proxy (e.g. LiteLLM) if you need it.

### The privacy trade, stated plainly

With `provider: openai` pointed at a hosted URL, your prompts (which
include your plans, outcomes, memories, and profile summary) leave your
machine for that provider. Everything else in ALFRED stays local: the
database, the audit log, the agents. Governance, allowlists, and tiers
behave identically whichever brain answers. Local-first remains the
default and the recommendation; a hosted brain is a trade you make with
eyes open.

## Per-agent model overrides

Any agent can pin its own generation settings in `manifest.yaml`; the
executor passes them through on every call for that agent:

```yaml
model:
  model: "qwen3:14b"     # must be available on the SAME configured backend
  temperature: 0.2
  max_tokens: 2048
```

All agents share the one configured backend (one `ModelPort`), so the
override picks a different model on that backend, not a different
provider. A practical split: meta agents (qa, scout) on a small fast
model, planning agents on the biggest one your machine holds.

## Troubleshooting

- `alfred doctor` first, always: it probes the backend, resolves the
  model name, and checks the key env var when the provider is `openai`.
- "could not reach": the server is not up, or `llm.host` is wrong. For
  OpenAI-dialect servers the base URL almost always ends in `/v1`.
- HTTP 401/403: the key in `llm.api_key_env` (default
  `ALFRED_LLM_API_KEY`) is missing, wrong, or lacks access to the model.
  The error names the env var; the key itself is never printed.
- HTTP 404 on a hosted provider: the model id in `llm.name` is not one
  the provider offers; copy the exact id from their catalog.
- Garbage or empty plans on a tiny model: move up a size tier, or lower
  `llm.temperature` (0.2 to 0.4 suits planning work).
- Retry churn in the logs (StructuredCallError after 3 attempts): the
  model is too weak for the schema or the temperature is too high; the
  8B-class defaults do not have this problem.
- Everything offline, no model at all: `alfred chat --fake` exercises the
  whole pipeline with a canned brain.
