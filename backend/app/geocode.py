"""backend/app/geocode.py

geocode_address(address) -- NYC Geoclient API v2 address lookup, for the
walking-navigation design's third location-input method (named stop via
find_stop, current location via geolocation, typed address via this).
Scoped to NYC only: this product's routing coverage (OTP's graph, GTFS
feeds) is NYC-only regardless, and Geoclient itself is NYC-scoped, so no
extra bounding-box/plausibility check is needed beyond what the API
already guarantees.

Real response shape confirmed live (see docs/superpowers/specs/
2026-09-10-walking-navigation-design.md): success is
results[0].response.{latitude, longitude} as real WGS84 floats -- NOT
the also-present xCoordinate/yCoordinate NY State Plane fields. No-match
is status: "REJECTED", results: []. Never raises for expected failure
modes (no match, HTTP error) -- geocode_address returns None and the
caller (find_address tool dispatch) surfaces an honest "couldn't find
that address" rather than guessing.
"""
import httpx

from app.config import settings

GEOCLIENT_URL = "https://api.nyc.gov/geoclient/v2/search.json"


async def geocode_address(address: str) -> dict | None:
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                GEOCLIENT_URL,
                params={"input": address, "subscription-key": settings.geoclient_api_key},
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPError:
        return None

    results = data.get("results") or []
    if not results:
        return None

    geo_response = results[0].get("response") or {}
    lat = geo_response.get("latitude")
    lon = geo_response.get("longitude")
    if lat is None or lon is None:
        return None
    return {"lat": lat, "lon": lon}
