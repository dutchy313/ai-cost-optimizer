---
name: llm-cost-and-rate-limits
description: Use when designing, building, reviewing or debugging an LLM application or AI agent for token cost, prompt caching, model routing, batching, subagent delegation, output limits, or rate limits (RPM, TPM, ITPM, OTPM, TPD, RPD, IPM, 429 or 529 errors, quota design).
---

# LLM Cost and Rate-Limit Engineering

Use this skill to make an AI application or agent cheaper, and to keep it inside provider and internal rate limits, without lowering output quality.

## The one idea behind every lever

A model predicts one token at a time, and each new token depends on every token before it. Without caching, every turn replays the whole conversation, so you pay again for history you have already paid for. Agents make this worse: each tool call is another round trip carrying the full context.

Every lever below is a way to avoid paying for the same work twice, or to avoid sending work to a model that code or a cheaper model could do.

## Workflow

Follow these steps in order. Do not jump to step 3 before step 1.

1. **Measure.** Get a baseline per feature or agent: requests, input tokens, cached input tokens, output tokens, model, latency, cost, and success rate. If the app does not log these yet, add logging first (see "Instrumentation").
2. **Apply the levers in this order:**
   1. caching structure
   2. output limits
   3. batch for offline work
   4. model routing
   5. code instead of model calls
   6. subagent delegation
   7. context hygiene

   The first three usually cut spend by more than half and carry the least quality risk.
3. **Enforce limits.** Add client-side rate limiting, retries and degradation (see "Rate limits").
4. **Verify.** Re-run the same traffic sample. Compare cost per successful outcome and quality, not just cost per token. A cheaper run that fails more often is not a saving.

When you report back, use the format in "Output format".

## Bundled scripts

Use these instead of computing anything yourself. They are in `scripts/` next to this file, need only the Python 3.10+ standard library, and print Markdown by default (`--format json` for machine-readable output).

| Script | Command | Gives you |
| --- | --- | --- |
| Usage analyser | `python3 scripts/analyze_usage.py usage.jsonl` | Cost by model and task, cache-hit rate, output share, 429 rate, cost per success, savings estimate per lever |
| Codebase scanner | `python3 scripts/scan_codebase.py src/` | Call sites missing `max_tokens` or `cache_control`, dynamic values in prompts, hard-coded top models, fixed-sleep retries, no batch use |
| Cache break-even | `python3 scripts/cache_breakeven.py --ttl 1h --prefix-tokens 8000 --reuses 2 --price 5` | Whether a cache pays, and the minimum number of hits |
| Rate limiting | `python3 scripts/ratelimit.py` (self-test); copy into the app | `TokenBucket`, `LimitSet`, `backoff_delay`, `CircuitBreaker`, `call_with_limits` |
| Prices | `scripts/pricing.json` | USD per million tokens per model class. Edit it to match your contracts |

The scanner is heuristic. Confirm each finding by reading the code before changing it. Savings estimates from the analyser overlap, so never add them together.

## Lever 1: Prompt caching

**Rules**

- Put static content first and dynamic content last: tools, then system prompt, then reference documents, then conversation history, then the new user message.
- Put the cache breakpoint at the end of the last static block.
- Never put a timestamp, request ID, user name or the incoming message before a breakpoint. One changed character invalidates everything after it.
- Keep tool definitions in a stable order. Reordering tools breaks the prefix.
- Read the cache fields in the usage block on every response, and alert if the cache-hit rate falls.

**Choose the cache lifetime (TTL) with the break-even formula**

```
N* = (w - r) / (1 - r)
```

Here `w` is the cache-write multiplier, `r` is the cache-read multiplier, and `N*` is the total number of times the prefix is used (the first write plus every cache hit) at which caching breaks even. Run `scripts/cache_breakeven.py` rather than working it out by hand.

Anthropic charges 1.25x to write a 5-minute cache, 2x to write a 1-hour cache, and 0.1x to read from either.

- **5-minute cache:** pays off from the second use.
- **1-hour cache:** needs at least three uses. A single reuse costs more than not caching at all.

**Know the window you are working against**

| Context | Typical cache lifetime |
| --- | --- |
| Anthropic API (default) | 5 minutes, reset on every hit |
| Anthropic API (extended) | 1 hour |
| OpenAI API | Automatic, roughly 5 minutes to 24 hours depending on model |
| Claude apps (subscription) | About 1 hour |

These values change. Check current provider documentation before relying on them.

**Anthropic example**

```python
import anthropic

client = anthropic.Anthropic()

response = client.messages.create(
    model=MODEL_TOP,
    max_tokens=1024,
    system=[
        {"type": "text", "text": STATIC_INSTRUCTIONS},
        {
            "type": "text",
            "text": REFERENCE_DOCS,
            # Breakpoint at the end of the static prefix
            "cache_control": {"type": "ephemeral"},
        },
    ],
    messages=history + [{"role": "user", "content": user_input}],  # dynamic content last
)

u = response.usage
log(
    cached_read=u.cache_read_input_tokens,
    cache_write=u.cache_creation_input_tokens,
    uncached_in=u.input_tokens,
    out=u.output_tokens,
)
```

For OpenAI, caching is automatic on repeated prefixes. Read `usage.prompt_tokens_details.cached_tokens` and apply the same static-first ordering.

**Caching also raises your rate limit.** On Anthropic, cached reads do not count toward input-tokens-per-minute (ITPM). With an 80% cache-hit rate, a 2M ITPM limit carries about 10M total input tokens per minute.

**Long-running sessions**

- Do the back-and-forth of a task inside the cache window. Do not leave a long session idle and then resume it.
- Before pausing a task for longer than the window, ask the model to summarise decisions, facts and open questions. Start the next session from that summary.

## Lever 2: Output control

Output tokens are the most expensive tokens.

- Set `max_tokens` to the smallest value the task needs. Never leave it at the model maximum.
- Return structured output (JSON with a schema) instead of prose wherever a program reads the result.
- Instruct brevity in the system prompt, for example "Answer in at most 3 sentences", or "Use Simplified Technical English (ASD-STE100)".
- Use stop sequences when the useful answer has a clear end.
- **OpenAI caveat:** OpenAI counts the full `max_tokens` against your tokens-per-minute (TPM) limit even when fewer tokens come back. A generous `max_tokens` wastes quota as well as money.
- Reasoning tokens are billed as output. Turn reasoning off, or set a low budget, for clerical tasks.

## Lever 3: Batch for offline work

- Send any request that does not need an answer within seconds to a batch API. This covers evals, data enrichment, bulk classification, nightly reports and backfills.
- Batch APIs typically cost about 50% less, use a separate rate-limit pool, and can take up to 24 hours to complete. Batch does not support streaming.
- On Anthropic, batch does not combine with fast mode.

```python
batch = client.messages.batches.create(requests=[
    {
        "custom_id": f"doc-{i}",
        "params": {
            "model": MODEL_LIGHT,
            "max_tokens": 256,
            "messages": [{"role": "user", "content": d}],
        },
    }
    for i, d in enumerate(docs)
])
# Poll client.messages.batches.retrieve(batch.id), then read the results.
```

- A daily scheduled job that builds on yesterday's output is a cheap form of agent loop. Prefer it to a loop that runs nonstop.

## Lever 4: Model routing

Use the cheapest model class that meets the quality bar for each task.

| Class | Example API IDs (verify current) | Price weight vs Top | Use for |
| --- | --- | --- | --- |
| Light | `claude-haiku-5-5` | 0.2 | Classification, tagging, extraction, short summaries, routing decisions |
| Middle | `claude-sonnet-5-5` | 0.6 | Drafting, everyday code, lighter analysis |
| Top | `claude-opus-5-5` | 1.0 | Deep analysis, planning, orchestrating other agents |
| Flagship | `claude-fable-5-1` | 2.0 | Named exceptions only |

**Rules**

- Tag every call with a task type, and route on the tag, not on guesswork.
- Start on the cheapest allowed class. Escalate one class only when a validator or confidence check fails ("cascade routing").
- Build a small evaluation harness. Run a sample of real traffic through the candidate models, and record cost and success rate for each. Set routing thresholds from that data, and review them monthly.
- Use a second model as a reviewer for important outputs. Different models have different blind spots.
- Keep routing in a gateway or a single client module, not scattered through the code.

## Lever 5: Code instead of model calls

If a step has one right answer, make it deterministic. This covers arithmetic, date maths, parsing, format conversion, validation, file reading and lookups.

- Ship scripts inside skills, so the model calls the script and spends almost no tokens on the work itself.
- Never ask a model to calculate totals, tax or percentages. It predicts digits rather than computing them, and it uses reasoning tokens to do it.
- For an agent that runs the same flow every day, put it on a coded backbone (for example LangGraph or CrewAI, or plain code). Use the model only at the steps that need judgement.

```python
# Bundled as scripts/add_vat.py in a skill; the model calls it, never computes it.
from decimal import Decimal, ROUND_HALF_UP

def add_vat(net: str, rate: str = "0.20") -> str:
    gross = Decimal(net) * (1 + Decimal(rate))
    return str(gross.quantize(Decimal("0.01"), ROUND_HALF_UP))
```

## Lever 6: Subagent delegation

Treat subagents as interns: give each one narrow job with just enough context.

- Good jobs to delegate: summarise this file, make one yes-or-no call, search this folder and return only the relevant snippets.
- Run subagents on the Light or Middle class.
- The subagent returns only the result. Raw files, search output and its working stay out of the orchestrator's context.
- **Timing rule:** delegate only jobs that reliably finish inside the orchestrator's cache window, which is 5 minutes on the Anthropic API by default. If the subagent takes longer, the orchestrator pays to recompute its whole context, and the delegation costs more than it saved. Send longer jobs to batch or a scheduled task.
- Never fan out a swarm for an open-ended task such as brainstorming. Cost scales with the number of agents.

## Lever 7: Context hygiene

- Keep system prompts and custom instructions short. Wrap sections in simple tags, such as `<style>…</style>` and `<format>…</format>`.
- Load only the tools and skills the task needs. Every tool definition is sent on every turn and adds paths for the model to wander.
- Start a new session when the task changes.
- Retrieve, don't dump: use retrieval or a memory tool that fetches only relevant chunks. Never paste whole documents "just in case".
- Prefer Markdown or HTML to Word files. Convert files before sending, or use a subagent to extract only the fields you need.

## Rate limits

### Dimensions

| Metric | Caps | Notes |
| --- | --- | --- |
| RPM | Requests per minute | Retries and failed requests count |
| TPM / ITPM / OTPM | Tokens per minute (combined, input, output) | Anthropic splits input and output; cached reads do not count toward ITPM |
| TPD | Tokens per day | |
| RPD | Requests per day | |
| IPM | Images generated per minute | |
| Concurrency | Requests in flight | The binding limit on self-hosted GPUs and on some providers |

A request is refused when **any** of these limits runs out.

**Provider behaviour to design for**

- Limits are usually per organisation and per model class, not per API key. Without internal quotas, one workload can starve all the others.
- Anthropic uses a token bucket that refills continuously. A 60 RPM limit behaves like 1 request per second, not 60 requests at the top of each minute.
- Read the rate-limit response headers (`anthropic-ratelimit-*` on Anthropic, `x-ratelimit-*` on OpenAI) and keep client-side state in sync with them.

### Client-side token bucket

Keep one bucket per limit dimension and model class. Count tokens before sending a request, so you don't spend quota on requests the provider will reject.

```python
import asyncio
import time

class TokenBucket:
    def __init__(self, capacity_per_min: float):
        self.capacity = capacity_per_min
        self.tokens = capacity_per_min
        self.rate = capacity_per_min / 60.0  # refill per second
        self.updated = time.monotonic()
        self.lock = asyncio.Lock()

    async def take(self, amount: float):
        if amount > self.capacity:
            raise ValueError("Request larger than bucket capacity; split it or raise the limit")
        async with self.lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= amount:
                    self.tokens -= amount
                    return
                await asyncio.sleep((amount - self.tokens) / self.rate)

# Before each call:
#   await rpm.take(1)
#   await itpm.take(uncached_input_estimate)
#   await otpm.take(max_tokens)
# After the call, credit back (max_tokens - actual_output) where the provider counts actual output.
```

Use `client.messages.count_tokens(...)` on Anthropic, or a tokenizer such as `tiktoken` on OpenAI, to estimate input size.

### Retries

```python
import random

def backoff_delay(attempt: int, retry_after: float | None,
                  base: float = 1.0, cap: float = 32.0) -> float:
    if retry_after is not None:
        return retry_after                                      # always honour the server's value
    return random.uniform(0, min(cap, base * 2 ** attempt))    # full jitter
```

- Retry only on 429, 529 and 5xx errors, with at most 5 attempts. Never retry other 4xx errors.
- Add a circuit breaker: after 10 failures in 60 seconds, stop calling for 60 seconds and serve the fallback.
- **Ramping up:** grow sustained traffic by no more than 50% every 15 minutes.
- **On sustained 503 or 529 errors:** halve the request rate, hold it, then step back up gradually.

### Degrade before refusing

| Usage of any limit | Action |
| --- | --- |
| Below 80% | Normal |
| 80–90% | Route down one model class where allowed; cut `max_tokens` by 25% |
| 90–100% | Queue low-priority traffic; move deferrable work to batch |
| 100% | Return 429 with a `Retry-After` header |

Every user-facing feature needs a fallback: a cached or templated answer, a Light-class path, or a "your result is queued" message.

### Fair access in multi-tenant systems

- Route all traffic through one gateway, and give each workload its own virtual key with a tier, quotas, a model allow-list and a monthly budget.
- Guaranteed shares across tiers must add up to no more than 80% of the provider ceiling, leaving headroom for bursts and retries.
- Use priority lanes, with critical work first and sandbox work last, and weighted fair queuing within each lane.
- **Self-hosted GPUs:**
  - Cap in-flight sequences per replica according to KV-cache memory.
  - Enforce `max_tokens` and context length on the server.
  - Refuse new requests when queue wait passes target, rather than letting latency grow for everyone.

## Instrumentation

Log these fields on every call:

```
timestamp, workload_key, task_type, model, input_tokens, cached_read_tokens,
cache_write_tokens, output_tokens, reasoning_tokens, latency_ms, status_code,
retries, routed_from, cost_usd, outcome_success
```

- Track cost per successful outcome, for example cost per resolved ticket or cost per accepted proposal.
- Alert on:
  - cache-hit rate dropping day on day
  - 429 rate above 2%
  - provider ceiling use above 85%
  - budget used reaching 50%, 80% and 100%
- Use OpenTelemetry with Langfuse or LangSmith, or an LLM gateway, to trace each token back to the prompt, tool call and workflow that caused it. Check what a tool actually exports before relying on it: "supports OpenTelemetry" can mean very little.

## Review checklist

Run this checklist when reviewing an AI application or agent:

- [ ] Static prompt content comes first, with a cache breakpoint after it and no dynamic values before it
- [ ] Cache-hit rate is logged and above 50% on steady traffic
- [ ] `max_tokens` is set per call and sized to the task
- [ ] Structured output is used where code consumes the result
- [ ] Offline or bulk work uses a batch API
- [ ] Every call carries a task type, and routing starts on the cheapest adequate class
- [ ] Reasoning is off or capped on clerical tasks
- [ ] No arithmetic, parsing or conversion is done by the model
- [ ] Subagents run on cheaper models, return only results, and finish inside the cache window
- [ ] Only the tools the task needs are loaded
- [ ] There is a client-side token bucket per dimension, tokens are pre-counted, and `Retry-After` is honoured with jittered backoff
- [ ] A degradation path and user-facing fallback exist
- [ ] Per-workload quotas and budgets are enforced at a gateway
- [ ] Prompts, skills and eval suites are portable across providers

## Anti-patterns

- Defaulting every call to the top model "to be safe".
- Putting a timestamp, user name or request ID in the system prompt.
- Using a 1-hour cache for a prefix that is used only once or twice.
- Retry loops without jitter, which synchronise into retry storms.
- Leaving `max_tokens` at the model maximum on OpenAI, which burns TPM quota.
- Pasting whole PDFs or Word files into context to read a few fields.
- Fanning out large numbers of subagents for open-ended tasks.
- Re-using one long chat across unrelated tasks.
- Measuring cost per token instead of cost per successful outcome.

## Output format

When advising on or reviewing a system, return:

1. **Baseline:** current spend and token mix, or what must be instrumented to get it.
2. **Findings:** each issue with its lever, estimated saving range, effort (low, medium or high) and quality risk.
3. **Changes:** concrete code or configuration diffs, in priority order.
4. **Limits plan:** quotas, buckets, retry and degradation settings.
5. **Verification:** the traffic sample and the metrics that will confirm the change worked.

## Reference numbers (as of October 2026; verify before use)

| Item | Value |
| --- | --- |
| Anthropic cache write, 5-minute TTL | 1.25x base input price |
| Anthropic cache write, 1-hour TTL | 2x base input price |
| Anthropic cache read | 0.1x base input price; not counted toward ITPM |
| OpenAI cache read | Up to 90% discount; the GPT-5.6 generation uses a 1.25x write / 0.1x read structure |
| Batch APIs (Anthropic, OpenAI, xAI) | About 50% discount, up to 24-hour turnaround, separate rate-limit pool |
| Tokens per English word | About 1.33, varying by tokenizer |
| Recommended ramp-up | No more than 50% growth every 15 minutes |
