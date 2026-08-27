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

# Haiku 4.5 pricing (USD per million tokens) — verify against
# https://platform.claude.com/docs/en/about-claude/pricing before trusting
# this for real budgeting; update here if pricing changes. Confirmed live
# 2026-08-24, including the prompt-caching multipliers below (a 5-minute
# cache write costs 1.25x base input; a cache hit costs 0.1x base input).
PRICE_PER_MTOK_INPUT_USD = 1.00
PRICE_PER_MTOK_OUTPUT_USD = 5.00
PRICE_PER_MTOK_CACHE_WRITE_USD = 1.25
PRICE_PER_MTOK_CACHE_READ_USD = 0.10

# How many multiples of the sustainable daily rate (cap / 30) count as
# "draining too fast" and trigger the alarm print.
DAILY_ALARM_MULTIPLIER = 3


class CostCapExceeded(RuntimeError):
    pass


def _hard_cap_usd() -> float:
    return float(os.environ.get("ANTHROPIC_MONTHLY_SPEND_CAP_USD", "5"))


def _cost_usd(usage: dict) -> float:
    input_tokens = usage.get("input_tokens", 0) or 0
    output_tokens = usage.get("output_tokens", 0) or 0
    # Present (as separate fields, not folded into input_tokens) whenever
    # prompt caching is in play -- 0/absent otherwise, so this is a no-op
    # for callers that don't use caching.
    cache_creation_tokens = usage.get("cache_creation_input_tokens", 0) or 0
    cache_read_tokens = usage.get("cache_read_input_tokens", 0) or 0
    return (input_tokens / 1_000_000) * PRICE_PER_MTOK_INPUT_USD + \
           (output_tokens / 1_000_000) * PRICE_PER_MTOK_OUTPUT_USD + \
           (cache_creation_tokens / 1_000_000) * PRICE_PER_MTOK_CACHE_WRITE_USD + \
           (cache_read_tokens / 1_000_000) * PRICE_PER_MTOK_CACHE_READ_USD


def _read_log() -> list[dict]:
    if not LOG_PATH.exists():
        return []
    with open(LOG_PATH, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _sum_cost_for_prefix(records: list[dict], ts_prefix: str) -> float:
    return sum(r["cost_usd"] for r in records if r["ts"].startswith(ts_prefix))


def log_call(usage: dict, agent: str) -> float:
    """Append a call's cost to the log; return this call's cost in USD.

    Raises CostCapExceeded if this call pushes month-to-date spend at or
    past the configured cap — callers should treat that as fatal for any
    further LLM calls this month.
    """
    cost = _cost_usd(usage)
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
