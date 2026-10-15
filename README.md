# AI Cost Optimizer agent

A Claude agent that audits an LLM application or AI agent for token cost and rate-limit risk. It returns a prioritised savings plan and, when you allow it, applies the fixes.

AI Cost Optimizer audits applications built on large language models for avoidable token spend and rate-limit failures. It reads your usage logs and source code, finds the places where calls waste money (missing prompt caching, no output limits, an oversized model for the task, unbatched offline work, naive retries), and returns a ranked savings plan with concrete code fixes.

Every number comes from a script. Costs, cache-hit rates and savings estimates are calculated by bundled Python scripts that need only the standard library, never guessed by the model.
The agent keeps its own costs down. A Sonnet agent decides what matters and hands narrow, read-only checks to a Haiku helper.
Safe defaults. By default it only writes a report. It can edit files only when you pass --apply. Web access is off, and a hard budget cap stops the run.
Use it however you like. Run it inside Claude Code, run it without the UI through the Claude Agent SDK, or use the scripts on their own.

It is built on the `llm-cost-and-rate-limits` skill and follows that skill's own rules:

- Python scripts do every count and every cost calculation.
- A cheap Haiku subagent confirms findings, one file at a time.
- The orchestrator runs on Sonnet and only does the judgement.

## What's inside

```
ai-cost-optimizer/
├── agent.py                         # headless runner (Claude Agent SDK)
├── requirements.txt
├── .claude/
│   ├── agents/
│   │   ├── ai-cost-optimizer.md     # orchestrator subagent (Sonnet, preloads the skill)
│   │   └── llm-callsite-checker.md  # narrow checker subagent (Haiku, read-only)
│   └── skills/llm-cost-and-rate-limits/
│       ├── SKILL.md                 # the playbook
│       └── scripts/
│           ├── analyze_usage.py     # cost, cache-hit rate, 429s, savings per lever from logs
│           ├── scan_codebase.py     # finds anti-patterns at LLM call sites
│           ├── cache_breakeven.py   # 5-minute vs 1-hour cache, or no cache
│           ├── ratelimit.py         # token bucket, jittered backoff, circuit breaker
│           └── pricing.json         # prices per model class (edit to match yours)
└── examples/
    ├── make_sample_log.py           # generates a synthetic usage log
    ├── usage_sample.jsonl
    └── sample_app/support_bot.py    # deliberately wasteful app for testing
```

## Use it in Claude Code

1. Copy `.claude/agents/` and `.claude/skills/` into your project's `.claude/` folder for one project, or into `~/.claude/` for every project.
2. In Claude Code, ask:

   ```text
   Use the ai-cost-optimizer agent to audit this app. Usage log: logs/usage.jsonl
   ```

   Or force it with `@"ai-cost-optimizer (agent)"`.

3. It reports first. To apply changes, say "apply the low-risk fixes".

To run a whole session as the agent, use `claude --agent ai-cost-optimizer`.

The agent keeps project memory in `.claude/agent-memory/ai-cost-optimizer/`. This holds the baseline, the levers applied and any false positives, so the next run starts from where the last one finished.

## Run it headless (Agent SDK)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...

# Try it on the bundled example
python agent.py --target examples/sample_app --logs examples/usage_sample.jsonl

# Your own app: report only, stop at $2
python agent.py --target ../my-llm-app --logs ../my-llm-app/logs/usage.jsonl

# Let it edit, lowest-risk levers first
python agent.py --target ../my-llm-app --apply --budget 3
```

The report goes to `cost-report.md`. The runner also prints the agent's own cost per model.

Safety defaults:

- Report-only mode cannot edit files. The `Edit` and `Write` tools are removed, and anything not pre-approved is denied.
- The agent can run only the bundled scripts through Bash.
- Web access is off.
- `--budget` sets a hard stop in USD.

## Use the scripts on their own

They need only the Python 3.10+ standard library.

```bash
S=.claude/skills/llm-cost-and-rate-limits/scripts
python3 $S/analyze_usage.py examples/usage_sample.jsonl   # cost report + savings by lever
python3 $S/scan_codebase.py examples/sample_app           # anti-patterns at call sites
python3 $S/cache_breakeven.py --ttl 1h                    # needs 3 uses (2 hits) to pay off
python3 $S/ratelimit.py                                   # self-test
```

### Usage log format

Use one JSON object per line, or one CSV row per call. Missing fields count as zero.

```
timestamp, workload_key, task_type, model, input_tokens, cached_read_tokens,
cache_write_tokens, output_tokens, reasoning_tokens, latency_ms, status_code,
retries, cost_usd, outcome_success, interactive
```

- `input_tokens` means uncached input, the same as Anthropic's `usage.input_tokens`. If your logs record total input, pass `--input-includes-cache`.
- `interactive: false` marks calls nobody waits for. These are batch candidates.
- If `cost_usd` is missing, cost is computed from `pricing.json`.

## Before you rely on the numbers

- `pricing.json` holds list prices per model class as of October 2026. Check them against your provider's pricing page and your contracts.
- The scanner is heuristic. The agent confirms each finding by reading the code, and you should too.
- Savings estimates overlap. For example, caching and routing touch the same input tokens, so don't add them together.
- Route traffic to a cheaper model only after running an eval on real traffic.
