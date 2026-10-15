#!/usr/bin/env python3
"""Static scan of a codebase for LLM cost and rate-limit anti-patterns.

Finds LLM call sites (Anthropic, OpenAI, LiteLLM, Vercel AI SDK, LangChain)
in Python, JavaScript and TypeScript files and checks each against the skill's
review checklist. It is a heuristic: every finding is a lead for the agent to
confirm by reading the code, not a verdict.

Usage:
  python3 scan_codebase.py PATH [--format md|json] [--max-files 5000]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

EXTS = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx"}
SKIP_DIRS = {"node_modules", ".git", ".venv", "venv", "env", "__pycache__", "dist", "build",
             ".next", ".tox", "site-packages", ".mypy_cache", ".pytest_cache", "coverage"}

CALL_RE = re.compile(
    r"(messages\.create|messages\.stream|chat\.completions\.create|responses\.create|"
    r"completions\.create|litellm\.(a?completion)|generateText|streamText|generateObject|"
    r"ChatAnthropic\(|ChatOpenAI\(|\.invoke\()"
)
BATCH_RE = re.compile(r"(batches\.create|/v1/batches|messages\.batches|batch_create)")
MODEL_RE = re.compile(r"""["'`]((?:claude|gpt|o\d|gemini|llama|mistral|deepseek|qwen|kimi|glm)[\w.\-:]*)["'`]""", re.I)
TOP_TOKENS = {"opus", "fable", "mythos", "sol", "pro", "ultra"}


def top_model_in(text: str) -> str | None:
    """Return the first model string whose name tokens mark it as top-tier."""
    for m in MODEL_RE.finditer(text):
        tokens = set(re.split(r"[-_.:/]+", m.group(1).lower()))
        if tokens & TOP_TOKENS:
            return m.group(1)
    return None
DYNAMIC_IN_PROMPT_RE = re.compile(
    r"(datetime\.now|datetime\.utcnow|time\.time\(|Date\.now\(|new Date\(|uuid4?\(|request_id|session_id|user_name|username)")
SYSTEM_RE = re.compile(r"(system\s*[=:]|\"system\"|'system'|role\s*[=:]\s*[\"']system)")
FIXED_SLEEP_RE = re.compile(r"(time\.sleep\(\s*\d+(\.\d+)?\s*\)|asyncio\.sleep\(\s*\d+(\.\d+)?\s*\)|setTimeout\([^,]+,\s*\d+\s*\))")
JITTER_RE = re.compile(r"(random|jitter|Math\.random|retry_after|retry-after|Retry-After|tenacity|backoff)")
RETRY_LOOP_RE = re.compile(r"(for\s+\w+\s+in\s+range\(|while\s+True|while\s*\(true\)|retry|attempt)", re.I)
ARITH_PROMPT_RE = re.compile(r"(calculate|compute|sum of|add up|total of|percentage|VAT|tax)", re.I)
TOOLS_KW_RE = re.compile(r"\btools\s*[=:]")

MAX_SPAN = 40   # longest call (in lines) the scanner will follow
LOOKBACK = 8    # lines before a call checked for a prompt built just above it


def strip_comments(line: str) -> str:
    """Drop full-line and trailing comments (heuristic; ignores quotes)."""
    s = line.lstrip()
    if s.startswith(("#", "//", "*", "/*")):
        return ""
    line = re.sub(r"\s#\s.*$", "", line)
    return re.sub(r"\s//\s.*$", "", line)


def call_span(lines: list[str], i: int) -> int:
    """Index one past the line where the call opened on line i closes."""
    depth, opened = 0, False
    for j in range(i, min(len(lines), i + MAX_SPAN)):
        for ch in lines[j]:
            if ch in "([{":
                depth += 1
                opened = True
            elif ch in ")]}":
                depth -= 1
        if opened and depth <= 0:
            return j + 1
    return min(len(lines), i + MAX_SPAN)


def iter_files(root: Path, max_files: int):
    count = 0
    for p in root.rglob("*"):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.is_file() and p.suffix in EXTS:
            yield p
            count += 1
            if count >= max_files:
                return


def finding(path, line, lever, severity, message, fix):
    return {"file": str(path), "line": line, "lever": lever, "severity": severity,
            "message": message, "fix": fix}


def scan_file(path: Path, root: Path):
    try:
        lines = [strip_comments(l) for l in path.read_text(encoding="utf-8", errors="ignore").splitlines()]
    except OSError:
        return [], {}
    rel = path.relative_to(root) if path.is_relative_to(root) else path
    text = "\n".join(lines)
    out = []
    stats = {"call_sites": 0, "models": Counter(), "batch": bool(BATCH_RE.search(text))}

    for m in MODEL_RE.finditer(text):
        stats["models"][m.group(1)] += 1

    prev_end = 0
    for i, line in enumerate(lines):
        if not CALL_RE.search(line):
            continue
        # `.invoke(` is generic; only count it when an LLM client is nearby.
        if ".invoke(" in line and not re.search(r"(llm|chat|model|chain)", line, re.I):
            continue
        stats["call_sites"] += 1
        lineno = i + 1
        end = call_span(lines, i)
        block = "\n".join(lines[i:end])                                   # the call's own arguments
        before = "\n".join(lines[max(prev_end, i - LOOKBACK):i])          # prompt built just above it, not the previous call
        prev_end = end
        ctx = before + "\n" + block

        if not re.search(r"(max_tokens|max_output_tokens|max_completion_tokens|maxTokens|maxOutputTokens)", block):
            out.append(finding(rel, lineno, "2 Output control", "high",
                               "LLM call without an explicit output cap",
                               "Set max_tokens to the smallest value the task needs; prefer structured output."))
        else:
            big = re.search(r"(max_tokens|maxTokens|max_completion_tokens)\s*[=:]\s*(\d+)", block)
            if big and int(big.group(2)) >= 8000:
                out.append(finding(rel, lineno, "2 Output control", "medium",
                                   f"max_tokens set to {big.group(2)}",
                                   "Size max_tokens to the task. On OpenAI the full cap counts against TPM."))

        is_anthropic = "messages." in line or "ChatAnthropic" in line or "anthropic" in ctx.lower()
        if is_anthropic and "cache_control" not in ctx and "cacheControl" not in ctx and SYSTEM_RE.search(ctx):
            out.append(finding(rel, lineno, "1 Prompt caching", "high",
                               "Anthropic call with a system prompt but no cache_control breakpoint",
                               "Put static content first and add cache_control at the end of the static prefix."))

        if SYSTEM_RE.search(ctx) and DYNAMIC_IN_PROMPT_RE.search(ctx):
            dyn = DYNAMIC_IN_PROMPT_RE.search(ctx).group(1).rstrip("(") + ("()" if "(" in DYNAMIC_IN_PROMPT_RE.search(ctx).group(1) else "")
            out.append(finding(rel, lineno, "1 Prompt caching", "medium",
                               f"Dynamic value ({dyn}) near the prompt; if it sits in the system prompt it breaks the cache prefix",
                               "Move timestamps, IDs and user names after the cache breakpoint, into the user turn."))

        top = top_model_in(block)
        if top:
            out.append(finding(rel, lineno, "4 Model routing", "medium",
                               f"Hard-coded top-tier model ({top}) at a call site",
                               "Route by task type from one config; start on the cheapest adequate class."))

        if ARITH_PROMPT_RE.search(block) and re.search(r"(content|prompt|messages)", block):
            out.append(finding(rel, lineno, "5 Code instead of model calls", "low",
                               "Prompt near this call mentions a calculation",
                               "Do arithmetic, tax and totals in code; let the model call a script."))

        tool_defs = len(re.findall(r"(input_schema|\"parameters\"|'parameters'|inputSchema)", block))
        if TOOLS_KW_RE.search(block) and tool_defs >= 8:
            out.append(finding(rel, lineno, "7 Context hygiene", "low",
                               f"{tool_defs} tool definitions sent with this call",
                               "Load only the tools this task needs; every definition is sent every turn."))

    for i, line in enumerate(lines):
        if FIXED_SLEEP_RE.search(line):
            window = "\n".join(lines[max(0, i - 12):i + 4])
            if RETRY_LOOP_RE.search(window) and not JITTER_RE.search(window):
                out.append(finding(rel, i + 1, "Rate limits", "high",
                                   "Retry with a fixed sleep and no jitter or Retry-After handling",
                                   "Honour Retry-After; otherwise exponential backoff with full jitter, max 5 attempts."))
    return out, stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", type=Path)
    ap.add_argument("--format", choices=("md", "json"), default="md")
    ap.add_argument("--max-files", type=int, default=5000)
    args = ap.parse_args(argv)
    root = args.path.resolve()
    if not root.exists():
        print(f"error: {root} not found", file=sys.stderr)
        return 2

    findings, call_sites, models, batch_used, files = [], 0, Counter(), False, 0
    paths = [root] if root.is_file() else list(iter_files(root, args.max_files))
    base = root.parent if root.is_file() else root
    for p in paths:
        files += 1
        f, s = scan_file(p, base)
        findings += f
        call_sites += s.get("call_sites", 0)
        models.update(s.get("models", {}))
        batch_used = batch_used or s.get("batch", False)

    if call_sites and not batch_used:
        findings.append(finding("(project)", 0, "3 Batch", "low",
                                "No batch API usage found anywhere in the project",
                                "Send evals, enrichment and nightly jobs to a batch API (about 50% cheaper, separate limits)."))
    sev_rank = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: (sev_rank[f["severity"]], f["file"], f["line"]))
    summary = {
        "files_scanned": files,
        "llm_call_sites": call_sites,
        "models_referenced": dict(models.most_common()),
        "batch_api_used": batch_used,
        "findings_by_severity": dict(Counter(f["severity"] for f in findings)),
        "findings": findings,
        "note": "Heuristic scan. Confirm each finding by reading the code before changing it.",
    }
    if args.format == "json":
        print(json.dumps(summary, indent=2))
        return 0
    print("# Codebase scan\n")
    print(f"Files scanned: {files}. LLM call sites: {call_sites}. Batch API used: {'yes' if batch_used else 'no'}.")
    if models:
        print("Models referenced: " + ", ".join(f"`{m}` ({n})" for m, n in models.most_common()) + ".")
    print("\nHeuristic scan: confirm each finding by reading the code.\n")
    print("| Severity | Location | Lever | Finding | Fix |")
    print("| --- | --- | --- | --- | --- |")
    for f in findings:
        loc = f"{f['file']}:{f['line']}" if f["line"] else f["file"]
        print(f"| {f['severity']} | {loc} | {f['lever']} | {f['message']} | {f['fix']} |")
    if not findings:
        print("| - | - | - | No anti-patterns matched | - |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
