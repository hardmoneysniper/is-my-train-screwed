"""Local safety net for Anthropic spend.

This is a *second* line of defense, not a replacement for the console-side
hard cap — the Anthropic API has no endpoint to set a spend limit
programmatically, so that cap must be set by hand at
console.anthropic.com (Settings > Limits). This module adds a local
circuit breaker on top: it logs every call's cost and (a) raises before
any further call once month-to-date spend reaches the configured cap, and
(b) prints an alarm if today's spend alone implies burning the monthly
budget far faster than a sustainable daily rate.

Usage: after every Anthropic API call, pass response.usage.model_dump()
(or an equivalent dict with input_tokens/output_tokens) to log_call().
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

LOG_PATH = Path(__file__).parent / "data" / "cost_log.ndjson"

# USD per million tokens, keyed by model id. Verify against
# https://platform.claude.com/docs/en/about-claude/pricing before trusting
# this for real budgeting; update here if pricing changes.
#
# Haiku 4.5 confirmed live 2026-08-24, including the prompt-caching
# multipliers (a 5-minute cache write costs 1.25x base input; a cache hit
# costs 0.1x base input). Sonnet 5 confirmed live 2026-10-17 ($2/$10,
# Anthropic's permanent standard rate after the planned Sept 2026 increase
# to $3/$15 was cancelled) -- its cache multipliers are assumed to follow
# the same 1.25x/0.1x-of-base-input ratio Haiku's docs confirm, not
# separately verified per-model (Anthropic prices caching proportionally
# to base input rate across its models).
#
# 2026-10-17 real fix: log_call() used to apply ONLY the Haiku rates to
# every call regardless of which model actually ran -- harmless while
# Haiku was the only model in use, but would have silently undercounted a
# Sonnet call's real cost by ~2-3x against the $5 hard cap the moment one
# was added (app/location_resolver.py, this same session). Caller must now
# pass the real model id; an unrecognized model id falls back to Haiku's
# rates with a printed warning rather than raising, so a typo'd model
# string degrades to an undercount (loud, not silent) instead of a crash.
_HAIKU_MODEL = "claude-haiku-4-5-20251001"
PRICING_PER_MTOK_USD: dict[str, dict[str, float]] = {
    _HAIKU_MODEL: {"input": 1.00, "output": 5.00, "cache_write": 1.25, "cache_read": 0.10},
    "claude-sonnet-5": {"input": 2.00, "output": 10.00, "cache_write": 2.50, "cache_read": 0.20},
}

# How many multiples of the sustainable daily rate (cap / 30) count as
# "draining too fast" and trigger the alarm print.
DAILY_ALARM_MULTIPLIER = 3


class CostCapExceeded(RuntimeError):
    pass


def _hard_cap_usd() -> float:
    return float(os.environ.get("ANTHROPIC_MONTHLY_SPEND_CAP_USD", "5"))


def _cost_usd(usage: dict, model: str) -> float:
    rates = PRICING_PER_MTOK_USD.get(model)
    if rates is None:
        print(f"[cost_guard] unrecognized model {model!r}, falling back to Haiku rates for cost tracking", flush=True)
        rates = PRICING_PER_MTOK_USD[_HAIKU_MODEL]
    input_tokens = usage.get("input_tokens", 0) or 0
    output_tokens = usage.get("output_tokens", 0) or 0
    # Present (as separate fields, not folded into input_tokens) whenever
    # prompt caching is in play -- 0/absent otherwise, so this is a no-op
    # for callers that don't use caching.
    cache_creation_tokens = usage.get("cache_creation_input_tokens", 0) or 0
    cache_read_tokens = usage.get("cache_read_input_tokens", 0) or 0
    return (input_tokens / 1_000_000) * rates["input"] + \
           (output_tokens / 1_000_000) * rates["output"] + \
           (cache_creation_tokens / 1_000_000) * rates["cache_write"] + \
           (cache_read_tokens / 1_000_000) * rates["cache_read"]


def _read_log() -> list[dict]:
    if not LOG_PATH.exists():
        return []
    with open(LOG_PATH, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _sum_cost_for_prefix(records: list[dict], ts_prefix: str) -> float:
    return sum(r["cost_usd"] for r in records if r["ts"].startswith(ts_prefix))


def log_call(usage: dict, agent: str, model: str = _HAIKU_MODEL) -> float:
    """Append a call's cost to the log; return this call's cost in USD.

    `model` must be the real model id the call actually ran against --
    defaults to Haiku only for backwards compatibility with callers that
    predate per-model pricing (2026-10-17); always pass it explicitly for
    any new call site.

    Raises CostCapExceeded if this call pushes month-to-date spend at or
    past the configured cap — callers should treat that as fatal for any
    further LLM calls this month.
    """
    cost = _cost_usd(usage, model)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent": agent,
            "cost_usd": cost,
            **usage,
        }) + "\n")

    _check_burn_rate()
    return cost


def _check_burn_rate() -> None:
    cap = _hard_cap_usd()
    records = _read_log()
    now = datetime.now(timezone.utc)
    mtd = _sum_cost_for_prefix(records, now.strftime("%Y-%m"))
    today = _sum_cost_for_prefix(records, now.strftime("%Y-%m-%d"))
    sustainable_daily = cap / 30

    if mtd >= cap:
        raise CostCapExceeded(
            f"Month-to-date Anthropic spend ${mtd:.4f} has reached the ${cap:.2f} local cap — "
            f"halting further calls. Confirm the console-side hard cap is also set at "
            f"console.anthropic.com under Settings > Limits."
        )
    if today >= sustainable_daily * DAILY_ALARM_MULTIPLIER:
        print(
            f"[cost_guard] ALARM: today's spend ${today:.4f} is {DAILY_ALARM_MULTIPLIER}x+ the "
            f"sustainable daily rate (${sustainable_daily:.4f}/day for a ${cap:.2f}/mo cap). "
            f"Burning too fast — investigate before it recurs.",
            flush=True,
        )
