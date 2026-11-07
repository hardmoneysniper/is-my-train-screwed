"""backend/app/find_place.py

find_place(query) -- Geoapify Geocoding API AUTOCOMPLETE search, the
third-tier location fallback (after find_stop and find_address both
return no match): resolves a free-text business/landmark name (e.g. "the
Zara on 21st St") to a list of real candidate matches, for cases too
vague for a transit-stop name or a literal street address.

2026-10-17 real endpoint fix: this originally called
/v1/geocode/search (plain structured-address search), which live-testing
showed is a poor fit for named-place lookups -- "hudson yards" matched
only a generic postal-code-level point, and appending more disambiguating
text made it WORSE (drifted to a same-named street near JFK, in Queens,
nowhere near Hudson Yards). /v1/geocode/autocomplete -- built specifically
for ranked, partial, user-typed queries -- handles the same raw query
correctly as its first-ranked result ("30 Hudson Yards, 30 Hudson
Boulevard East..."). Also live-confirmed autocomplete returns ZERO
results for an unmodified full sentence ("the zara on 21st st
manhattan") -- it needs concise phrasing, not prose; that's why
app/location_resolver.py's job is to strip filler words into a short
business/landmark-style string, not rewrite into a full address sentence.

Returns a LIST of up to 5 candidates, not a single top pick: live-testing
showed a short, common query ("zara manhattan") can legitimately match
several real, equally-plausible stores at once (5 distinct real Zara
locations across Manhattan) -- silently returning only the highest-ranked
one would be exactly the kind of guessing this product's "never guess"
discipline exists to avoid. The caller (ConversationAgent.SYSTEM_PROMPT)
is responsible for showing multiple candidates to the user and asking
which one they meant, same spirit as the single-match confirmation flow.

Scoped to NYC via a strict bounding-box filter (same bbox trim_osm_extract.py
already uses for this product's OTP street-graph trim -- re-used here for
consistency, not re-derived) since this product's routing coverage is
NYC-only regardless. Never raises for expected failure modes (no match,
HTTP error) -- returns an empty list and the caller surfaces an honest
"couldn't find that place" rather than guessing.

2026-11-06: every non-empty result (including a single candidate -- see
module docstring's own documented "Zara" vs. "Shoe Repair" failure,
which was exactly a single, confidently-wrong top match) now also passes
through `app.agents.location_quality_agent.check_location_matches`
before being returned, dropping any candidate that plainly doesn't match
what the rider described. Still never invents a coordinate -- it can
only drop a bad candidate, never substitute one of its own (see that
module's docstring for why).
"""
import httpx

from app.agents.location_quality_agent import check_location_matches
from app.config import settings

GEOAPIFY_URL = "https://api.geoapify.com/v1/geocode/autocomplete"
# lon1,lat1,lon2,lat2 -- matches trim_osm_extract.py's NYC bbox.
NYC_BBOX_FILTER = "rect:-74.08,40.49,-73.70,40.85"
MAX_CANDIDATES = 5


async def find_place(query: str, original_query: str | None = None) -> list[dict]:
    """`query` is what's actually sent to Geoapify -- typically
    location_resolver.py's cleaned-up rewrite, since that's what geocodes
    reliably (see module docstring). `original_query` -- the rider's own
    free-text description, before that rewrite -- is what the post-fetch
    quality check compares candidates against instead, since that's the
    real signal for rider intent; defaults to `query` when omitted, so a
    direct call bypassing location_resolver (e.g. in tests, or a caller
    with no separate original string) still behaves sensibly."""
    if original_query is None:
        original_query = query
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                GEOAPIFY_URL,
                params={
                    "text": query,
                    "apiKey": settings.geoapify_api_key,
                    "filter": NYC_BBOX_FILTER,
                    "limit": MAX_CANDIDATES,
                },
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPError:
        return []

    candidates = []
    for feature in data.get("features") or []:
        props = feature.get("properties") or {}
        lat = props.get("lat")
        lon = props.get("lon")
        formatted = props.get("formatted")
        if lat is None or lon is None or formatted is None:
            continue
        candidates.append({"lat": lat, "lon": lon, "formatted": formatted})
    if not candidates:
        return candidates
    return await check_location_matches(original_query, candidates)
