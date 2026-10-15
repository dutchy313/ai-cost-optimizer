#!/usr/bin/env python3
"""Generate a synthetic usage log (examples/usage_sample.jsonl) for trying the agent.

The data is invented. It mimics a support app with common waste patterns:
a top-tier model used for ticket tagging, an uncached system prompt, long
answers, a nightly report run interactively, and some 429s.
"""
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

random.seed(42)
OUT = Path(__file__).with_name("usage_sample.jsonl")
start = datetime(2026, 10, 1, tzinfo=timezone.utc)

PROFILES = [
    # task_type, model, n, (in_lo, in_hi), cache_hit, (out_lo, out_hi), reasoning_ratio, interactive
    ("ticket_tagging", "claude-opus-5-5", 600, (1800, 2400), 0.0, (20, 60), 1.5, True),
    ("support_answer", "claude-sonnet-5-5", 900, (6000, 9000), 0.15, (900, 1600), 0.2, True),
    ("nightly_report", "claude-opus-5-5", 60, (20000, 30000), 0.0, (2000, 3000), 0.3, False),
    ("doc_extraction", "claude-haiku-5-5", 400, (3000, 5000), 0.6, (100, 250), 0.0, True),
    (None, "claude-sonnet-5-5", 120, (1000, 2000), 0.0, (200, 500), 0.0, True),
]

rows = []
for task, model, n, (ilo, ihi), hit, (olo, ohi), rr, inter in PROFILES:
    for _ in range(n):
        total_in = random.randint(ilo, ihi)
        cread = int(total_in * hit)
        cwrite = int(total_in * 0.05) if hit else 0
        out = random.randint(olo, ohi)
        status = 429 if random.random() < 0.04 else 200
        rows.append({
            "timestamp": (start + timedelta(seconds=random.randint(0, 14 * 86400))).isoformat(),
            "workload_key": "support-app",
            "task_type": task,
            "model": model,
            "input_tokens": total_in - cread - cwrite,
            "cached_read_tokens": cread,
            "cache_write_tokens": cwrite,
            "output_tokens": out if status == 200 else 0,
            "reasoning_tokens": int(out * rr) if status == 200 else 0,
            "latency_ms": random.randint(400, 9000),
            "status_code": status,
            "retries": random.choice([0, 0, 0, 1, 2]) if status == 429 else 0,
            "outcome_success": status == 200 and random.random() > 0.05,
            "interactive": inter,
        })
rows.sort(key=lambda r: r["timestamp"])
OUT.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
print(f"wrote {len(rows)} rows to {OUT}")
