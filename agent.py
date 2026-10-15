#!/usr/bin/env python3
"""Run the AI cost-reduction agent on a codebase, headless, with the Claude Agent SDK.

Examples:
  python agent.py --target ../my-llm-app
  python agent.py --target ../my-llm-app --logs ../my-llm-app/logs/usage.jsonl
  python agent.py --target ../my-llm-app --apply --budget 3
  python agent.py --target examples/sample_app --logs examples/usage_sample.jsonl   # demo

By default the agent only reads and reports. --apply lets it edit files, lowest-risk
levers first. The run stops when it reaches --budget (USD, client-side estimate).

Requires: pip install claude-agent-sdk   and   ANTHROPIC_API_KEY in the environment.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

from claude_agent_sdk import (
    AgentDefinition,
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    ResultMessage,
    TextBlock,
    query,
)

PKG = Path(__file__).resolve().parent
AGENTS = PKG / ".claude" / "agents"
SKILL = "llm-cost-and-rate-limits"
SCRIPTS = PKG / ".claude" / "skills" / SKILL / "scripts"


def split_frontmatter(path: Path) -> tuple[dict, str]:
    """Minimal frontmatter reader (flat `key: value` pairs only) so we need no YAML dependency."""
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "-")):
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta, m.group(2).strip()


def build_options(args) -> ClaudeAgentOptions:
    main_meta, main_prompt = split_frontmatter(AGENTS / "ai-cost-optimizer.md")
    chk_meta, chk_prompt = split_frontmatter(AGENTS / "llm-callsite-checker.md")

    system_prompt = (
        main_prompt
        + f"\n\n## This run\n\nThe scripts are at `{SCRIPTS}`. Call them with `python3 {SCRIPTS}/<script>.py`."
        + ("\nYou MAY edit files in the target (the user passed --apply)." if args.apply
           else "\nREPORT ONLY: do not edit any file. Return the report as your final message.")
    )

    allowed = ["Read", "Grep", "Glob", "Agent", "Skill", f"Bash(python3 {SCRIPTS}/*)"]
    disallowed = ["WebFetch", "WebSearch"]
    if args.apply:
        allowed += ["Edit", "Write"]
    else:
        disallowed += ["Edit", "Write", "NotebookEdit"]

    return ClaudeAgentOptions(
        system_prompt=system_prompt,          # short custom prompt: cheaper than the full preset
        cwd=str(args.target),
        add_dirs=[str(PKG)],                  # makes the skill and its scripts reachable
        setting_sources=["project"],
        skills=[SKILL],
        agents={
            "llm-callsite-checker": AgentDefinition(
                description=chk_meta.get("description", "Confirms scan findings in one file"),
                prompt=chk_prompt,
                tools=["Read", "Grep"],
                model="haiku",                # the cheap intern
                maxTurns=6,
            ),
        },
        allowed_tools=allowed,
        disallowed_tools=disallowed,
        permission_mode="acceptEdits" if args.apply else "dontAsk",
        model=args.model or main_meta.get("model", "sonnet"),
        max_turns=args.max_turns,
        max_budget_usd=args.budget,
    )


def build_prompt(args) -> str:
    parts = [f"Audit the LLM application in `{args.target}` for token cost and rate-limit risk."]
    if args.logs:
        parts.append(f"Its usage log is `{args.logs}`. Run analyze_usage.py on it first.")
    else:
        parts.append("No usage log was given. Look for one; if there is none, say so and make instrumentation the first change.")
    if args.focus:
        parts.append(f"Focus on: {args.focus}.")
    parts.append("Apply the fixes after reporting them." if args.apply else "Report only.")
    return " ".join(parts)


async def run(args) -> int:
    options = build_options(args)
    final_text, result = [], None
    try:
        async for msg in query(prompt=build_prompt(args), options=options):
            if isinstance(msg, AssistantMessage) and msg.parent_tool_use_id is None:
                for block in msg.content:
                    if isinstance(block, TextBlock) and args.verbose:
                        print(block.text, file=sys.stderr)
            elif isinstance(msg, ResultMessage):
                result = msg
                if msg.result:
                    final_text.append(msg.result)
    except ClaudeSDKError as e:
        print(f"agent run failed: {e}", file=sys.stderr)
        return 1

    report = "\n".join(final_text).strip() or "(no report returned; see the run summary below)"
    out = Path(args.out).resolve()
    out.write_text(report + "\n", encoding="utf-8")
    print(f"Report written to {out}")

    if result is not None:
        cost = result.total_cost_usd or 0.0
        print(f"Run: {result.subtype}, {result.num_turns} turns, ${cost:.4f} estimated")
        for model, u in (result.model_usage or {}).items():
            print(f"  {model}: in {u.get('inputTokens', 0)}, cache-read {u.get('cacheReadInputTokens', 0)}, "
                  f"out {u.get('outputTokens', 0)}, ${u.get('costUSD', 0):.4f}")
        if result.subtype == "error_max_budget_usd":
            print("Stopped at the budget cap; raise --budget or narrow --focus.", file=sys.stderr)
            return 3
        if result.is_error:
            return 1
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", type=Path, required=True, help="root of the LLM application to audit")
    ap.add_argument("--logs", type=Path, help="usage log (JSONL or CSV)")
    ap.add_argument("--focus", help="optional focus, e.g. 'caching only' or 'the chat endpoint'")
    ap.add_argument("--apply", action="store_true", help="let the agent edit files")
    ap.add_argument("--model", help="orchestrator model (default from the agent file: sonnet)")
    ap.add_argument("--budget", type=float, default=2.0, help="hard stop in USD (default 2.00)")
    ap.add_argument("--max-turns", type=int, default=40)
    ap.add_argument("--out", default="cost-report.md")
    ap.add_argument("--verbose", action="store_true", help="stream the agent's narration to stderr")
    args = ap.parse_args(argv)

    args.target = args.target.resolve()
    if not args.target.exists():
        ap.error(f"target {args.target} does not exist")
    if args.logs:
        args.logs = args.logs.resolve()
        if not args.logs.exists():
            ap.error(f"log {args.logs} does not exist")
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
