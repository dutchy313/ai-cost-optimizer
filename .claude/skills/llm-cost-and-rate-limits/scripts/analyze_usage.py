#!/usr/bin/env python3
"""Deterministic LLM usage-log analyser.

Reads a JSONL or CSV usage log (one row per model call) and reports cost,
token mix, cache-hit rate, error rates and estimated savings per lever.
The model never does this arithmetic: it calls this script and reads the result.

Expected fields per row (missing numeric fields count as 0):
  timestamp, workload_key, task_type, model, input_tokens, cached_read_tokens,
  cache_write_tokens, output_tokens, reasoning_tokens, latency_ms, status_code,
  retries, cost_usd, outcome_success, interactive

`input_tokens` means UNCACHED input (the Anthropic convention). If your log
records total input including cache reads, pass --input-includes-cache.
`interactive` (true/false) marks whether a user waits on the answer; rows with
interactive=false are batch candidates.

Usage:
  python3 analyze_usage.py LOG [--pricing pricing.json] [--format md|json]
                               [--input-includes-cache] [--top 10]
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
NUM_FIELDS = (
    "input_tokens", "cached_read_tokens", "cache_write_tokens", "output_tokens",
    "reasoning_tokens", "latency_ms", "retries",
)


# ---------- loading ----------

def load_rows(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    rows: list[dict] = []
    if path.suffix.lower() in (".jsonl", ".ndjson", ".json"):
        stripped = text.lstrip()
        if stripped.startswith("["):
            rows = json.loads(text)
        else:
            for i, line in enumerate(text.splitlines(), 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as e:
                    print(f"warning: skipping line {i}: {e}", file=sys.stderr)
    else:
        rows = list(csv.DictReader(text.splitlines()))
    return rows


def to_num(v) -> float:
    if v is None or v == "":
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def to_bool(v, default=None):
    if v is None or v == "":
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "y", "t")


# ---------- pricing ----------

class Pricing:
    def __init__(self, path: Path):
        cfg = json.loads(path.read_text(encoding="utf-8"))
        self.m = cfg["multipliers"]
        self.classes = cfg["classes"]
        self.patterns = cfg["model_class_patterns"]
        self.default = cfg.get("default_class", "middle")

    def model_class(self, model: str) -> str:
        tokens = set(re.split(r"[-_.:/\s]+", (model or "").lower()))
        for needle, cls in self.patterns:
            if needle in tokens:
                return cls
        return self.default

    def cost(self, cls: str, inp, cread, cwrite, out, batch=False) -> float:
        p = self.classes[cls]
        c = (
            inp * p["input"]
            + cread * p["input"] * self.m["cache_read"]
            + cwrite * p["input"] * self.m["cache_write_5m"]
            + out * p["output"]
        ) / 1_000_000
        return c * (self.m["batch"] if batch else 1.0)


# ---------- aggregation ----------

def blank():
    return defaultdict(float)


def aggregate(rows, pricing: Pricing, input_includes_cache: bool):
    total = blank()
    by_model = defaultdict(blank)
    by_task = defaultdict(blank)
    task_class = defaultdict(lambda: defaultdict(int))
    noninteractive = blank()

    for r in rows:
        model = str(r.get("model", "unknown"))
        task = str(r.get("task_type") or "untagged")
        cls = pricing.model_class(model)
        n = {f: to_num(r.get(f)) for f in NUM_FIELDS}
        if input_includes_cache:
            n["input_tokens"] = max(0.0, n["input_tokens"] - n["cached_read_tokens"] - n["cache_write_tokens"])
        status = int(to_num(r.get("status_code")) or 200)
        success = to_bool(r.get("outcome_success"), default=(status < 400))
        logged_cost = r.get("cost_usd")
        cost = to_num(logged_cost) if logged_cost not in (None, "") else pricing.cost(
            cls, n["input_tokens"], n["cached_read_tokens"], n["cache_write_tokens"], n["output_tokens"])
        out_cost = n["output_tokens"] * pricing.classes[cls]["output"] / 1_000_000

        for bucket in (total, by_model[model], by_task[task]):
            bucket["requests"] += 1
            bucket["cost"] += cost
            bucket["output_cost"] += out_cost
            bucket["success"] += 1 if success else 0
            bucket["rate_limited"] += 1 if status in (429, 529) else 0
            bucket["errors"] += 1 if status >= 400 else 0
            for f in NUM_FIELDS:
                bucket[f] += n[f]
        by_model[model]["_class"] = cls  # type: ignore[assignment]
        task_class[task][cls] += 1

        if to_bool(r.get("interactive"), default=True) is False:
            noninteractive["requests"] += 1
            noninteractive["cost"] += cost
    return total, by_model, by_task, task_class, noninteractive


def derived(b) -> dict:
    total_in = b["input_tokens"] + b["cached_read_tokens"] + b["cache_write_tokens"]
    req = b["requests"] or 1
    return {
        "requests": int(b["requests"]),
        "cost_usd": round(b["cost"], 4),
        "input_tokens_uncached": int(b["input_tokens"]),
        "cached_read_tokens": int(b["cached_read_tokens"]),
        "cache_write_tokens": int(b["cache_write_tokens"]),
        "output_tokens": int(b["output_tokens"]),
        "reasoning_tokens": int(b["reasoning_tokens"]),
        "cache_hit_rate": round(b["cached_read_tokens"] / total_in, 3) if total_in else 0.0,
        "output_cost_share": round(b["output_cost"] / b["cost"], 3) if b["cost"] else 0.0,
        "avg_input_tokens": round(total_in / req),
        "avg_output_tokens": round(b["output_tokens"] / req),
        "rate_limited_rate": round(b["rate_limited"] / req, 4),
        "error_rate": round(b["errors"] / req, 4),
        "success_rate": round(b["success"] / req, 4),
        "cost_per_success_usd": round(b["cost"] / b["success"], 6) if b["success"] else None,
        "avg_retries": round(b["retries"] / req, 3),
    }


# ---------- savings estimates ----------

def estimate_savings(total, by_model, by_task, task_class, noninteractive, pricing: Pricing):
    findings = []
    t = derived(total)
    spend = t["cost_usd"] or 1e-9

    # Lever 1: caching. Assume a stable prefix could reach a 70% hit rate.
    target_hit = 0.70
    for model, b in by_model.items():
        d = derived(b)
        if d["avg_input_tokens"] >= 1024 and d["cache_hit_rate"] < 0.5 and d["requests"] >= 10:
            cls = b["_class"]
            total_in = b["input_tokens"] + b["cached_read_tokens"] + b["cache_write_tokens"]
            new_read = total_in * target_hit
            new_uncached = total_in - new_read
            before = pricing.cost(cls, b["input_tokens"], b["cached_read_tokens"], b["cache_write_tokens"], 0)
            after = pricing.cost(cls, new_uncached, new_read, 0, 0)
            saving = max(0.0, before - after)
            if saving > 0:
                findings.append({
                    "lever": "1 Prompt caching",
                    "scope": f"model {model}",
                    "evidence": f"cache-hit rate {d['cache_hit_rate']:.0%} on avg {d['avg_input_tokens']} input tokens/request",
                    "estimated_saving_usd": round(saving, 4),
                    "assumption": f"static prefix restructured to reach a {target_hit:.0%} hit rate",
                    "effort": "low", "quality_risk": "none",
                })

    # Lever 2: output. Flag long average outputs and reasoning on light work.
    for task, b in by_task.items():
        d = derived(b)
        if d["avg_output_tokens"] > 800 and d["requests"] >= 10:
            cls_counts = task_class[task]
            cls = max(cls_counts, key=cls_counts.get)
            trimmed = b["output_tokens"] * 0.4
            saving = trimmed * pricing.classes[cls]["output"] / 1_000_000
            findings.append({
                "lever": "2 Output control",
                "scope": f"task {task}",
                "evidence": f"avg {d['avg_output_tokens']} output tokens/request; output is {d['output_cost_share']:.0%} of this task's cost",
                "estimated_saving_usd": round(saving, 4),
                "assumption": "tighter max_tokens, structured output and a brevity instruction cut output by 40%",
                "effort": "low", "quality_risk": "low",
            })
        if b["reasoning_tokens"] > 0.3 * b["output_tokens"] and d["avg_output_tokens"] < 400 and d["requests"] >= 10:
            cls_counts = task_class[task]
            cls = max(cls_counts, key=cls_counts.get)
            saving = b["reasoning_tokens"] * 0.8 * pricing.classes[cls]["output"] / 1_000_000
            findings.append({
                "lever": "2 Output control (reasoning)",
                "scope": f"task {task}",
                "evidence": f"reasoning tokens are {b['reasoning_tokens'] / max(b['output_tokens'], 1):.0%} of output on short answers",
                "estimated_saving_usd": round(saving, 4),
                "assumption": "reasoning switched off or capped for this task type",
                "effort": "low", "quality_risk": "medium - verify with evals",
            })

    # Lever 3: batch.
    if noninteractive["requests"]:
        saving = noninteractive["cost"] * (1 - pricing.m["batch"])
        findings.append({
            "lever": "3 Batch",
            "scope": f"{int(noninteractive['requests'])} non-interactive requests",
            "evidence": f"${noninteractive['cost']:.2f} spent on calls nobody waits for",
            "estimated_saving_usd": round(saving, 4),
            "assumption": "moved to a batch API at the batch discount",
            "effort": "medium", "quality_risk": "none",
        })

    # Lever 4: routing. Short, simple calls on top or flagship models.
    for task, b in by_task.items():
        d = derived(b)
        heavy = task_class[task].get("top", 0) + task_class[task].get("flagship", 0)
        if heavy and d["avg_output_tokens"] <= 300 and d["requests"] >= 10 and task != "untagged":
            share = heavy / d["requests"]
            heavy_cost = b["cost"] * share
            light = pricing.classes["light"]
            ref = pricing.classes["top"]
            ratio = (light["input"] + light["output"]) / (ref["input"] + ref["output"])
            saving = heavy_cost * (1 - ratio)
            findings.append({
                "lever": "4 Model routing",
                "scope": f"task {task}",
                "evidence": f"{share:.0%} of {d['requests']} calls run on top/flagship models with avg output {d['avg_output_tokens']} tokens",
                "estimated_saving_usd": round(saving, 4),
                "assumption": "routed to the light class, escalating only on validator failure; confirm with an eval",
                "effort": "medium", "quality_risk": "medium - verify with evals",
            })
    if "untagged" in by_task:
        findings.append({
            "lever": "4 Model routing (prerequisite)",
            "scope": "task untagged",
            "evidence": f"{int(by_task['untagged']['requests'])} calls carry no task_type, so they cannot be routed",
            "estimated_saving_usd": 0.0,
            "assumption": "tag every call before routing",
            "effort": "low", "quality_risk": "none",
        })

    # Rate limits.
    if t["rate_limited_rate"] > 0.02:
        findings.append({
            "lever": "Rate limits",
            "scope": "all traffic",
            "evidence": f"{t['rate_limited_rate']:.1%} of calls returned 429/529 (target below 2%); avg {t['avg_retries']} retries/call",
            "estimated_saving_usd": 0.0,
            "assumption": "add a client-side token bucket, jittered backoff and caching to raise effective ITPM",
            "effort": "medium", "quality_risk": "none",
        })

    findings.sort(key=lambda f: f["estimated_saving_usd"], reverse=True)
    for f in findings:
        f["share_of_spend"] = round(f["estimated_saving_usd"] / spend, 3)
    return findings


# ---------- output ----------

def to_markdown(summary: dict, top: int) -> str:
    t = summary["total"]
    lines = [
        "# LLM usage analysis",
        "",
        f"Rows analysed: {t['requests']}. Total cost: ${t['cost_usd']:.2f}. "
        f"Cache-hit rate: {t['cache_hit_rate']:.0%}. Output share of cost: {t['output_cost_share']:.0%}. "
        f"429/529 rate: {t['rate_limited_rate']:.1%}. Success rate: {t['success_rate']:.0%}.",
        "",
        "Savings estimates overlap (for example, caching and routing touch the same input tokens), so do not add them up.",
        "",
        "## Findings by estimated saving",
        "",
        "| Lever | Scope | Evidence | Est. saving (USD) | Share of spend | Effort | Quality risk |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for f in summary["findings"][:top]:
        lines.append(
            f"| {f['lever']} | {f['scope']} | {f['evidence']} | {f['estimated_saving_usd']:.2f} | "
            f"{f['share_of_spend']:.0%} | {f['effort']} | {f['quality_risk']} |")
    if not summary["findings"]:
        lines.append("| - | - | No lever crossed its threshold | 0 | 0% | - | - |")

    def table(title, data, key_name):
        out = ["", f"## {title}", "",
               f"| {key_name} | Requests | Cost (USD) | Cache hit | Avg in | Avg out | Output share | Success | Cost/success |",
               "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for k, d in sorted(data.items(), key=lambda kv: kv[1]["cost_usd"], reverse=True)[:top]:
            cps = f"{d['cost_per_success_usd']:.4f}" if d["cost_per_success_usd"] is not None else "n/a"
            out.append(f"| {k} | {d['requests']} | {d['cost_usd']:.2f} | {d['cache_hit_rate']:.0%} | "
                       f"{d['avg_input_tokens']} | {d['avg_output_tokens']} | {d['output_cost_share']:.0%} | "
                       f"{d['success_rate']:.0%} | {cps} |")
        return out

    lines += table("Cost by model", summary["by_model"], "Model")
    lines += table("Cost by task type", summary["by_task"], "Task type")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log", type=Path)
    ap.add_argument("--pricing", type=Path, default=HERE / "pricing.json")
    ap.add_argument("--format", choices=("md", "json"), default="md")
    ap.add_argument("--input-includes-cache", action="store_true")
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args(argv)

    if not args.log.exists():
        print(f"error: {args.log} not found", file=sys.stderr)
        return 2
    rows = load_rows(args.log)
    if not rows:
        print("error: no rows found", file=sys.stderr)
        return 2
    pricing = Pricing(args.pricing)
    total, by_model, by_task, task_class, nonint = aggregate(rows, pricing, args.input_includes_cache)
    summary = {
        "total": derived(total),
        "by_model": {m: {**derived(b), "class": b["_class"]} for m, b in by_model.items()},
        "by_task": {k: derived(b) for k, b in by_task.items()},
        "findings": estimate_savings(total, by_model, by_task, task_class, nonint, pricing),
        "pricing_as_of": json.loads(args.pricing.read_text())["as_of"],
    }
    if args.format == "json":
        print(json.dumps(summary, indent=2))
    else:
        print(to_markdown(summary, args.top))
    return 0


if __name__ == "__main__":
    sys.exit(main())
