"""backend/app/location_resolver.py

resolve_location_description(query) -- a one-shot Sonnet call that
rewrites a free-text, possibly-vague NYC place description into a
clearer, more specific search string before it's handed to find_place's
real Geoapify geocoder. Text-in/text-out only: this never returns
coordinates itself and never skips the real geocoder call -- the actual
lat/lon still always comes from find_place's live API response, never
invented here. This keeps the "LLM agents never compute numbers, routes,
or probabilities" rule intact: rewriting a search STRING is not computing
a coordinate.

Why this exists (2026-10-17): find_place's raw query, unmodified, can miss
or mismatch real NYC landmarks -- live-verified before building this,
"hudson yards" alone matched only a generic postal-code-level point, not
the actual landmark. A disambiguating rewrite (adding the borough,
clarifying it's a well-known landmark/development, standardizing phrasing)
can plausibly fix cases like that. It CANNOT fix a true data gap in the
underlying geocoder dataset -- live-verified the same day, "the zara on
21st st manhattan" matched an unrelated "Shoe Repair" business, because
Geoapify's OSM-backed index apparently has no accurate point for that
specific store; no amount of query rewriting invents data the geocoder's
database doesn't have. The existing confirm-before-route flow (see
ConversationAgent.SYSTEM_PROMPT) remains the real safety net for exactly
that failure mode.

Deliberately conservative: the system prompt instructs the model to add
only stable, well-known context it's genuinely confident about (borough,
neighborhood, landmark name) and never invent a precise street
number/address it isn't sure of -- a wrong-but-confident-sounding rewrite
would be worse than passing the user's original wording through
unchanged, since it could steer the geocoder toward a different wrong
match without the user having any reason to doubt it.
"""
from anthropic import AnthropicError

from app.anthropic_text import call_and_log, extract_text
from app.config import settings

SYSTEM_PROMPT = (
    "You normalize free-text NYC place descriptions into a short search "
    "string for a geocoder autocomplete endpoint -- it needs concise "
    "phrasing, NOT a full sentence (live-tested: a full sentence like "
    "'the zara on 21st st manhattan' returns zero results; 'Zara, "
    "Manhattan' returns real matches). Strip filler words ('the', 'on', "
    "'near', 'by') and keep only the business/landmark name plus a "
    "borough or neighborhood if you're confident of one -- 2-5 words is "
    "typical. Clarify a well-known landmark's full proper name if the "
    "user's wording is a nickname or abbreviation. Only add stable, "
    "well-known facts you're genuinely confident about. Never invent a "
    "precise street address or building number you aren't sure is correct "
    "-- if you have no confident detail to add, just strip the filler "
    "words and leave the rest unchanged rather than guessing. Respond "
    "with ONLY the short search string, nothing else -- no explanation, "
    "no quotes, no punctuation around it."
)


async def resolve_location_description(query: str) -> str:
    """Returns a rewritten search string, or `query` unchanged if the
    rewrite call fails for an API-level reason (network error, rate limit,
    etc.) -- this is a best-effort enrichment step, never a hard dependency
    for find_place to still work. Does NOT catch CostCapExceeded: a cap
    breach must still halt further calls, same as every other Anthropic
    call site in this product (cost_guard.log_call's own docstring)."""
    try:
        response = await call_and_log(
            settings.location_resolver_model, max_tokens=100,
            messages=[{"role": "user", "content": query}],
            agent="location_resolver", system=SYSTEM_PROMPT,
        )
    except AnthropicError:
        return query

    text = extract_text(response).strip()
    return text if text else query
