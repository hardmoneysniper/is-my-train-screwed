"""backend/app/agents/location_quality_agent.py

check_location_matches(original_query, candidates) -- a small Haiku call
that filters find_place.py's raw Geoapify candidates down to the ones
that genuinely match what the rider meant. Exists to catch exactly the
failure mode location_resolver.py's own docstring already documented
live: Geoapify's autocomplete can rank a confident-looking but WRONG
business first (e.g. "the zara on 21st st manhattan" matched an
unrelated "Shoe Repair" listing) with nothing in the response signaling
that anything went wrong. This module is a second, model-based check
behind that gap -- not a fix for the underlying geocoder data gap itself
(still a real coverage hole in Geoapify's OSM-backed index, same as
location_resolver.py's docstring explains), but a filter that stops an
obviously-wrong candidate from ever reaching the rider as if it were a
confident match.

"Correct if it is not correct" (2026-11-06, user) is interpreted
narrowly, consistent with this product's hard "never guess coordinates"
rule: this agent can only DROP a candidate it judges wrong, never invent
or substitute a coordinate of its own. If every candidate is judged
wrong, find_place's caller sees an honest empty list -- the same
"couldn't find that place" path it already takes for a genuine zero-
result search -- rather than a plausible-but-fabricated fix.
"""
import json

from anthropic import AnthropicError, AsyncAnthropic

import cost_guard
from app.config import settings

SYSTEM_PROMPT = (
    "A rider of a NYC transit app typed a free-text place description. A "
    "geocoder then returned candidate address matches for it. Decide "
    "which candidates, if any, are plausible real matches for what the "
    "rider meant -- a candidate is a BAD match if its name/address "
    "clearly describes a different kind of place or business than the "
    "rider's description (e.g. the rider asked for a clothing store and "
    "the candidate is a shoe repair shop), even if the geocoder ranked it "
    "first. Keep a candidate if it's a reasonable, even if imperfect, "
    "match -- only drop ones that are clearly wrong.\n\n"
    "Respond with ONLY a JSON object: "
    "{\"keep_indices\": [list of 0-based indices of candidates to keep, "
    "in their original relative order]}. An empty list is a valid answer "
    "if none of the candidates are plausible."
)


def _build_prompt(original_query: str, candidates: list[dict]) -> str:
    listed = "\n".join(f"{i}: {c['formatted']}" for i, c in enumerate(candidates))
    return f"Rider's description: {original_query}\n\nCandidates:\n{listed}"


def _parse_json_object(text: str) -> dict:
    """Asked for JSON-only output, but a model occasionally wraps it in
    prose or a markdown fence anyway -- extract the first {...} block
    rather than assuming the response text is bare JSON."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object found in model output: {text!r}")
    return json.loads(text[start:end + 1])


async def check_location_matches(original_query: str, candidates: list[dict]) -> list[dict]:
    """Returns the subset of `candidates` judged to genuinely match
    `original_query`, in their original relative order -- possibly empty
    if the model judges none of them are plausible (an intentional, real
    result, not a failure). Fails OPEN (returns `candidates` unfiltered)
    only on an actual check failure -- an API-level error, or a response
    that didn't parse as expected -- since this is a best-effort
    enhancement on top of find_place, never a hard dependency for it to
    keep working."""
    try:
        client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        response = await client.messages.create(
            model=settings.location_quality_model,
            max_tokens=200,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _build_prompt(original_query, candidates)}],
        )
        cost_guard.log_call(
            response.usage.model_dump(), agent="location_quality_agent", model=settings.location_quality_model,
        )
        text_block = next((b for b in response.content if b.type == "text"), None)
        result = _parse_json_object(text_block.text if text_block is not None else "")
        keep_indices = result.get("keep_indices")
        if not isinstance(keep_indices, list):
            return candidates
        return [candidates[i] for i in keep_indices if isinstance(i, int) and 0 <= i < len(candidates)]
    except (AnthropicError, ValueError):
        return candidates
