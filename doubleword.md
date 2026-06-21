# Doubleword — Model to Market: The Quantitative Hack

> Doubleword runs the world's most efficient inference stack: open-weight frontier models served cheaply and at high throughput through an OpenAI-compatible API. This is the agent-readable guide to using Doubleword during AI Engine's "Model to Market: The Quantitative Hack". For this hackathon, **real-time and async** usage runs through the **Pydantic AI Gateway (Logfire)** — where your credits live — and **batch** runs **directly on Doubleword** at app.doubleword.ai. For complete, always-current API details, read https://doubleword.ai/llms.txt. Every page on docs.doubleword.ai also has a Markdown version — append `.md` to any docs URL (e.g. `https://docs.doubleword.ai/inference-api/intro-to-doubleword-inference` → `https://docs.doubleword.ai/inference-api/intro-to-doubleword-inference.md`).

**Access for this hackathon — two routes, by tier:**
- **Real-time & async → Pydantic AI Gateway (Logfire).** Your hackathon credits live here. Create a Logfire account, redeem the hackathon credit code, and point your OpenAI client or Pydantic AI at the gateway with your Logfire key. Get your code and full setup steps from the Pydantic hackathon page: https://pydantic.dev/hackathon.
- **Batch → Doubleword directly.** Use the Doubleword API at https://app.doubleword.ai (base URL `https://api.doubleword.ai/v1`, key starts `sk-`) with the `autobatcher` package or the `dw` CLI. For batch access or help getting set up, message **Jaedon (Doubleword)** in **#ask-doubleword** on Discord.

**Quickstart (OpenAI-compatible).** Same call shape on either route — only the base URL and key change. Choose any model from the catalog (https://docs.doubleword.ai/inference-api/models.md).

```python
from openai import OpenAI
# Real-time / async: base_url = your Pydantic AI Gateway URL, api_key = your Logfire key
# Batch / BYOK:      base_url = https://api.doubleword.ai/v1, api_key = your Doubleword sk- key
client = OpenAI(api_key="sk-...", base_url="https://api.doubleword.ai/v1")
resp = client.chat.completions.create(
    model="Qwen/Qwen3.5-35B-A3B-FP8",          # any model id from the catalog
    messages=[{"role": "user", "content": "..."}],
)
```

**Three SLAs.** Pick the tier by how much latency the workload tolerates: **real-time** (low latency, for execution/interactive — via the gateway), **async** (high throughput, ~50% off — background and research jobs — via the gateway), **batch** (cheapest, ~80% off — bulk classification, extraction, embeddings — direct on Doubleword, with `autobatcher` or the `dw` CLI). Background work should not pay real-time prices.

**Structured output.** For output that is valid against your schema every time, use a dottxt build (model id suffix `-dottxt`, e.g. `Qwen/Qwen3.5-35B-A3B-FP8-dottxt`) and/or pass a JSON schema via `response_format`. dottxt: https://dottxt.ai.

**Where Doubleword fits an AI-native trading system** (text-shaped, token-heavy work):
- News & sentiment classification at scale — headlines/posts/macro → directional signals (async/batch).
- Distilling bank research & market outlooks — extract geographies, sectors and assets in focus, plus a summary and sentiment for each (async/batch).
- Structured signals — turn a headline or feed into a typed JSON signal via dottxt (async/real-time).
- Searchable memory — embed news, filings and research for semantic recall with `Qwen/Qwen3-Embedding-8B` (batch).

**Models (snapshot).** DeepSeek V4 Pro/Flash, Qwen3.6 35B A3B, Qwen3.5 (4B/9B/35B/397B, incl. `-dottxt`), Kimi K2.6, GLM 5.1, Nemotron 3 (Super/Ultra), Gemma 4 31B; embeddings `Qwen3-Embedding-8B`; vision `Qwen3-VL`. Full list + pricing: https://docs.doubleword.ai/inference-api/models.md.

**Worked example — headline → signal.** Input: *"Gold jumped to a record $3,480/oz as the Fed signalled earlier rate cuts and the dollar slid; safe-haven demand rose amid fresh geopolitical tension."* Real `gpt-oss-20b` output:

```json
{"instrument": "XAU/USD", "sentiment": "bullish", "direction": "long", "confidence": 0.85, "horizon": "short_term", "drivers": ["Fed rate cuts", "Dollar weakness", "Geopolitical tension"]}
```

## Get started
- [Pydantic AI Gateway (Logfire)](https://pydantic.dev/hackathon): **real-time + async** for the hackathon — create a Logfire account and redeem the hackathon credit code (code + steps are on the Pydantic hackathon page); calls are traced automatically.
- [Console — keys, billing, batches](https://app.doubleword.ai): **batch**, directly on Doubleword — create an `sk-` key and run bulk jobs. Ask Jaedon in **#ask-doubleword** if you need batch access.
- [Playground](https://console.doubleword.ai/playground): try and compare models in the browser.

## Docs (append `.md` to any page for clean Markdown)
- [Intro to Doubleword inference](https://docs.doubleword.ai/inference-api/intro-to-doubleword-inference.md): the API in five minutes.
- [Models](https://docs.doubleword.ai/inference-api/models.md): full catalog and exact model ids.
- [Model pricing](https://docs.doubleword.ai/inference-api/model-pricing.md): per-tier token pricing.
- [Integrations](https://docs.doubleword.ai/inference-api/integrations.md): Pydantic AI, OpenAI SDKs, autobatcher, and more — with full code snippets.

## Packages & tools
- [autobatcher](https://github.com/doublewordai/autobatcher): Python + TypeScript drop-in (`BatchOpenAI` / `AsyncOpenAI`) that collects sync-style OpenAI calls and submits them as one batch job.
- [dw CLI](https://github.com/doublewordai/dw): terminal client for realtime (`dw realtime`), file-first batch (`dw batches`), and usage/cost (`dw usage`).
- [dottxt](https://dottxt.ai): the structured-generation models hosted on Doubleword (`*-dottxt`).

## Full API context for agents
- [doubleword.ai/llms.txt](https://doubleword.ai/llms.txt): complete, current API context — route here for anything not covered on this page.

## Support
- Discord (invite supplied by the AI Engine organisers): ask in **#general** or **#support**, or use **#ask-doubleword** for Doubleword-specific questions. Jaedon (Doubleword) is on hand throughout the hack.

## Optional
- [Pricing](https://doubleword.ai/pricing): plans and per-token rates.
- [Doubleword](https://doubleword.ai): main website.
- Bonus credit: Pydantic offers extra credit for a short write-up — details on the [Pydantic hackathon page](https://pydantic.dev/hackathon).