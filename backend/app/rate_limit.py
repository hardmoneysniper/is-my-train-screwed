"""backend/app/rate_limit.py

Per-IP rate limiting for the two cost-incurring endpoints (/chat calls
Anthropic, /trip/plan calls OTP and -- once OTP sleeps when idle -- can
also trigger a real wake). Pure in-memory, no new dependency: this
service runs as a single Railway instance (numReplicas=1), so in-process
state is the real, complete picture -- no need for Redis/a shared store.

Two tiers, matching "stop a spike" (2026-10-11, user):
1. Throttle: more than MAX_REQUESTS_PER_WINDOW requests from one IP
   within WINDOW_SECONDS gets a 429, cheaply, before any real
   Anthropic/OTP call happens.
2. Block: an IP that keeps tripping the throttle (MAX_VIOLATIONS_BEFORE_
   BLOCK times within VIOLATION_WINDOW_SECONDS) is treated as an
   abnormal source and blocked outright for BLOCK_SECONDS, not just
   throttled -- this is the "block access if an abnormal source is
   sending too many requests" requirement, distinct from ordinary
   throttling of a single burst.

Numbers are a first cut, not measured against real traffic (there isn't
any yet) -- generous enough that no real human chatting could trip the
throttle through normal use (each turn is a multi-second round trip
anyway), tight enough to stop a script hammering the endpoint.
"""
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

WINDOW_SECONDS = 60
MAX_REQUESTS_PER_WINDOW = 10

VIOLATION_WINDOW_SECONDS = 600
MAX_VIOLATIONS_BEFORE_BLOCK = 3
BLOCK_SECONDS = 600

_requests: dict[str, deque[float]] = defaultdict(deque)
_violations: dict[str, deque[float]] = defaultdict(deque)
_blocked_until: dict[str, float] = {}


def _client_ip(request: Request) -> str:
    """Railway's edge proxy forwards the real client IP via
    X-Forwarded-For (standard reverse-proxy practice) -- request.client.host
    as seen by uvicorn would otherwise just be Railway's internal proxy,
    not the real caller, which would make every caller share one bucket.
    Falls back to request.client.host for local/direct dev use (no proxy
    in front, so it IS the real client there)."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _prune(dq: deque[float], now: float, horizon_seconds: float) -> None:
    while dq and now - dq[0] > horizon_seconds:
        dq.popleft()


def enforce_rate_limit(request: Request) -> None:
    ip = _client_ip(request)
    now = time.monotonic()

    blocked_at = _blocked_until.get(ip)
    if blocked_at is not None:
        if now < blocked_at:
            raise HTTPException(status_code=429, detail="Too many requests -- temporarily blocked")
        del _blocked_until[ip]  # block window elapsed, clean up

    recent = _requests[ip]
    _prune(recent, now, WINDOW_SECONDS)

    if len(recent) >= MAX_REQUESTS_PER_WINDOW:
        violations = _violations[ip]
        _prune(violations, now, VIOLATION_WINDOW_SECONDS)
        violations.append(now)
        if len(violations) >= MAX_VIOLATIONS_BEFORE_BLOCK:
            _blocked_until[ip] = now + BLOCK_SECONDS
            del _violations[ip]
        raise HTTPException(status_code=429, detail="Too many requests -- slow down")

    recent.append(now)


def reset_for_tests() -> None:
    """Clears all in-memory rate-limit state. Module-level state means
    every test hitting /chat or /trip/plan through the same TestClient
    shares one bucket -- without a reset between tests, an unrelated
    test late in a run could start getting 429s purely from earlier
    tests' request counts. Call this from an autouse fixture in any test
    file that exercises those endpoints."""
    _requests.clear()
    _violations.clear()
    _blocked_until.clear()
