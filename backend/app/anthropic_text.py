"""backend/app/anthropic_text.py

Shared helpers for the simple, single-turn Anthropic call pattern used
by every small agent in this product EXCEPT conversation_agent.py (whose
multi-turn tool-use loop is genuinely more complex and stays
hand-written). Extracted 2026-11-11: `call_and_log` consolidates a
construct-client/call/cost_guard.log_call block that was independently
duplicated, identically, across app/location_resolver.py,
app/agents/feedback_triage_agent.py, and app/agents/location_quality_agent.py.
`extract_text`'s one-liner was ALSO independently duplicated across those
3 plus conversation_agent.py (which still uses it directly, for its own
loop); `extract_json_object` across 2 of the 3.
"""
import json

from anthropic import AsyncAnthropic

import cost_guard
from app.config import settings


async def call_and_log(model: str, max_tokens: int, messages: list[dict], agent: str, system: str | None = None):
    """Makes one Anthropic call and logs its cost (cost_guard.log_call,
    which raises CostCapExceeded if this call pushes month-to-date spend
    over the configured cap -- left to propagate, never caught here,
    same as every pre-existing call site this replaces). Returns the raw
    response; callers that need parsed output still call extract_text/
    extract_json_object themselves, since what shape they want (plain
    text vs. a JSON object) differs per caller."""
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    kwargs = {"model": model, "max_tokens": max_tokens, "messages": messages}
    if system is not None:
        kwargs["system"] = system
    response = await client.messages.create(**kwargs)
    cost_guard.log_call(response.usage.model_dump(), agent=agent, model=model)
    return response


def extract_text(response) -> str:
    """The first text content block's text, or "" if the response has
    none at all (e.g. a tool-use-only turn)."""
    block = next((b for b in response.content if b.type == "text"), None)
    return block.text if block is not None else ""


def extract_json_object(text: str) -> dict:
    """Extracts the first {...} JSON object from model output text. A
    model asked for JSON-only output occasionally wraps it in prose or a
    markdown fence anyway, so this looks for the outermost brace pair
    rather than assuming the text is already bare JSON. Raises
    ValueError (never a silently-empty dict) if no object is found, so
    each caller's own try/except decides how a malformed response
    should degrade."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object found in model output: {text!r}")
    return json.loads(text[start:end + 1])
