#!/usr/bin/env python3
"""Prompt-cache break-even and cost comparison.

Break-even: N* = (w - r) / (1 - r) total uses of the same prefix
  (one cache write plus N* - 1 cache hits), where
  w = cache-write multiplier, r = cache-read multiplier (both vs base input price).
  With w=1.25, r=0.1 (5-minute cache): N* = 1.28, so 2 uses (1 hit) already save.
  With w=2.0,  r=0.1 (1-hour cache):   N* = 2.11, so you need 3 uses (2 hits).

Usage:
  python3 cache_breakeven.py --prefix-tokens 8000 --reuses 12 --price 5.0
  python3 cache_breakeven.py --prefix-tokens 8000 --reuses 2 --price 5.0 --ttl 1h
  python3 cache_breakeven.py --write 1.25 --read 0.1          # break-even only
"""
from __future__ import annotations

import argparse
import json
import math
import sys

TTL_WRITE = {"5m": 1.25, "1h": 2.0}


def breakeven(w: float, r: float) -> float:
    if r >= 1:
        return math.inf
    return (w - r) / (1 - r)


def compare(prefix_tokens: int, reuses: int, price_per_m: float, w: float, r: float) -> dict:
    """Cost of sending the same prefix (1 + reuses) times, with and without caching."""
    sends = 1 + reuses
    base = prefix_tokens * price_per_m / 1_000_000
    uncached = sends * base
    cached = base * w + reuses * base * r
    return {
        "sends": sends,
        "uncached_usd": round(uncached, 6),
        "cached_usd": round(cached, 6),
        "saving_usd": round(uncached - cached, 6),
        "saving_pct": round((uncached - cached) / uncached * 100, 1) if uncached else 0.0,
        "worth_caching": cached < uncached,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ttl", choices=TTL_WRITE, default="5m", help="sets --write when --write is not given")
    ap.add_argument("--write", type=float, help="cache-write multiplier")
    ap.add_argument("--read", type=float, default=0.1, help="cache-read multiplier")
    ap.add_argument("--prefix-tokens", type=int)
    ap.add_argument("--reuses", type=int, help="cache hits after the first write (total uses = reuses + 1)")
    ap.add_argument("--price", type=float, help="base input price, USD per million tokens")
    args = ap.parse_args(argv)

    w = args.write if args.write is not None else TTL_WRITE[args.ttl]
    n_star = breakeven(w, args.read)
    # N* counts total uses of the prefix (the first write plus every hit).
    finite = math.isfinite(n_star)
    min_uses = math.floor(n_star) + 1 if finite else None  # first whole count strictly cheaper than no cache
    result = {"write_multiplier": w, "read_multiplier": args.read,
              "breakeven_total_uses_N_star": round(n_star, 3) if finite else None,
              "min_total_uses_to_save": min_uses,
              "min_cache_hits_to_save": (min_uses - 1) if finite else None}
    if None not in (args.prefix_tokens, args.reuses, args.price):
        result["comparison"] = compare(args.prefix_tokens, args.reuses, args.price, w, args.read)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
