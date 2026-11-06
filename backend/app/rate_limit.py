"""backend/app/rate_limit.py

Rate limiting for the two cost-incurring endpoints (/chat calls
Anthropic, /trip/plan calls OTP and -- once OTP sleeps when idle -- can
also trigger a real wake). Pure in-memory, no new dependency: this
service runs as a single Railway instance (numReplicas=1), so in-process
state is the real, complete picture -- no need for Redis/a shared store.

Each limiter applies the same two tiers, matching "stop a spike"
(2026-10-11, user):
1. Throttle: more than `max_requests` requests from one key within
   `window_seconds` gets a 429, cheaply, before any real Anthropic/OTP
   call happens.
2. Block: a key that keeps tripping the throttle (`max_violations` times
   within `violation_window_seconds`) is treated as an abnormal source
   and blocked outright for `block_seconds`, not just throttled.

Three limiters, not one (2026-11-06, user: beta testers on the same
network share one IP, so a single shared per-IP bucket could lock out an
entire building over a handful of people chatting concurrently):
- `_trip_plan_limiter`: per client IP, /trip/plan's original (unchanged)
  threshold -- kept separate and untouched so fixing /chat's shared-IP
  problem doesn't also quietly loosen the limit in front of the one
  endpoint that can trigger a real-money OTP wake (app/otp_lifecycle.py).
- `_chat_ip_limiter`: per client IP, more generous -- a backstop against
  a script hammering /chat from one source, sized so a shared-IP group
  of real humans chatting normally won't trip it.
- `_user_limiter`: tighter, per `anonymous_id` -- catches one abusive
  real /chat user without punishing everyone behind their IP. Weaker as
  a pure abuse defense on its own (anonymous_id is client-supplied,
  trivially reset), which is exactly why it runs ALONGSIDE the IP
  backstop rather than instead of it. Its key space is fully attacker-
  controlled (unlike an IP), so `_Limiter` caps how many distinct keys
  it will track at once -- see MAX_TRACKED_KEYS below.
"""
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

# A key space an attacker fully controls (anonymous_id, not an IP) could
# otherwise grow these dicts unboundedly by sending a fresh value on
# every request. Rejecting unreasonably long keys bounds each entry's
# size; capping total tracked keys bounds the dict's overall size by
# resetting (losing only rate-limit *state*, never anything else) once
# it's clearly being abused rather than tracking real distinct users.
MAX_KEY_LENGTH = 128
MAX_TRACKED_KEYS = 5000


class _Limiter:
    """One throttle+block bucket, keyed by an arbitrary string (an IP or
    an anonymous_id) -- the same two-tier logic, parameterized, so the IP
    and per-user limiters below share one implementation instead of two
    copies of it."""

    def __init__(
        self,
        window_seconds: float,
        max_requests: int,
        violation_window_seconds: float,
        max_violations: int,
        block_seconds: float,
    ) -> None:
        self.window_seconds = window_seconds
        self.max_requests = max_requests
        self.violation_window_seconds = violation_window_seconds
        self.max_violations = max_violations
        self.block_seconds = block_seconds
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._violations: dict[str, deque[float]] = defaultdict(deque)
        self._blocked_until: dict[str, float] = {}

    @staticmethod
    def _prune(dq: deque[float], now: float, horizon_seconds: float) -> None:
        while dq and now - dq[0] > horizon_seconds:
            dq.popleft()

    def _evict_one_to_make_room(self) -> None:
        """Drops exactly one tracked key -- the oldest (dicts preserve
        insertion order) that is NOT currently blocked -- to free one
        slot. Deliberately evicts only one, not every evictable key: at
        5000 real distinct beta users, wiping all of them at once just
        because the 5001st showed up would be a far bigger blast radius
        than the point of this cap (bounding memory, not periodically
        mass-resetting real users' state). Never touches a blocked key,
        so an attacker churning fake keys can't use this to erase a real
        block. If every tracked key happens to be blocked (a large,
        coordinated, sustained attack -- not this limiter's threat
        model, see the module docstring), this is a no-op and the dict
        is allowed to grow past MAX_TRACKED_KEYS until a block expires
        and makes that key evictable again -- self-healing, not a hard
        bound, which is an accepted tradeoff for staying this simple."""
        for other_key in self._requests:
            if other_key not in self._blocked_until:
                del self._requests[other_key]
                self._violations.pop(other_key, None)
                return

    def check(self, key: str) -> None:
        if key not in self._requests and len(self._requests) >= MAX_TRACKED_KEYS:
            self._evict_one_to_make_room()

        now = time.monotonic()

        blocked_at = self._blocked_until.get(key)
        if blocked_at is not None:
            if now < blocked_at:
                raise HTTPException(status_code=429, detail="Too many requests -- temporarily blocked")
            del self._blocked_until[key]  # block window elapsed, clean up

        recent = self._requests[key]
        self._prune(recent, now, self.window_seconds)

        if len(recent) >= self.max_requests:
            violations = self._violations[key]
            self._prune(violations, now, self.violation_window_seconds)
            violations.append(now)
            if len(violations) >= self.max_violations:
                self._blocked_until[key] = now + self.block_seconds
                del self._violations[key]
            raise HTTPException(status_code=429, detail="Too many requests -- slow down")

        recent.append(now)

    def reset_for_tests(self) -> None:
        self._requests.clear()
        self._violations.clear()
        self._blocked_until.clear()


# Numbers are a first cut, not measured against real traffic -- generous
# enough that no real human (or small shared-IP group of them) chatting
# normally could trip it, tight enough to stop a script. Shared between
# both limiters except max_requests, which is deliberately different
# (see the class docstrings above for why).
VIOLATION_WINDOW_SECONDS = 600
MAX_VIOLATIONS_BEFORE_BLOCK = 3
BLOCK_SECONDS = 600

TRIP_PLAN_WINDOW_SECONDS = 60
TRIP_PLAN_MAX_REQUESTS = 10  # unchanged from the original single-tier
# limiter -- /trip/plan can trigger a real-cost OTP wake, so this stays
# at its original, more conservative ceiling rather than inheriting
# /chat's more generous one.

CHAT_IP_WINDOW_SECONDS = 60
CHAT_IP_MAX_REQUESTS = 30

USER_WINDOW_SECONDS = 60
USER_MAX_REQUESTS = 10  # same number the original single-tier limiter
# used -- tighter than the chat IP tier since it isolates one real user
# instead of a whole shared network.

def _make_limiter(window_seconds: float, max_requests: int) -> _Limiter:
    """A _Limiter using this module's shared block-tier policy
    (VIOLATION_WINDOW_SECONDS/MAX_VIOLATIONS_BEFORE_BLOCK/BLOCK_SECONDS)
    -- every limiter in this module uses the same block tier and only
    ever varies the throttle tier, so this is the one place that policy
    is wired in rather than three call sites repeating it by hand."""
    return _Limiter(
        window_seconds=window_seconds, max_requests=max_requests,
        violation_window_seconds=VIOLATION_WINDOW_SECONDS,
        max_violations=MAX_VIOLATIONS_BEFORE_BLOCK, block_seconds=BLOCK_SECONDS,
    )


_trip_plan_limiter = _make_limiter(TRIP_PLAN_WINDOW_SECONDS, TRIP_PLAN_MAX_REQUESTS)
_chat_ip_limiter = _make_limiter(CHAT_IP_WINDOW_SECONDS, CHAT_IP_MAX_REQUESTS)
_user_limiter = _make_limiter(USER_WINDOW_SECONDS, USER_MAX_REQUESTS)


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


def enforce_rate_limit(request: Request) -> None:
    """/trip/plan's limiter -- IP-only (no stable per-user identity to
    key on), at its original, cost-conscious threshold."""
    _trip_plan_limiter.check(_client_ip(request))


async def enforce_chat_rate_limit(request: Request) -> None:
    """/chat's limiter: a more generous IP backstop, plus a tighter per-
    anonymous_id check so one real abusive user can't exhaust a whole
    shared-IP group's quota. Reads anonymous_id directly from the
    request body (Starlette caches it, so this doesn't consume what the
    route handler's own ChatRequest parsing reads later) rather than
    requiring it as a typed dependency, since it must run before the
    body is otherwise parsed. A malformed/non-object body is left alone
    here -- this dependency only tries to extract anonymous_id for the
    per-user tier, never to validate the request; FastAPI's own body
    parsing produces the real 422 for bad input once this returns.

    A missing/empty/oversized anonymous_id -- or a body that isn't valid
    JSON at all -- falls back to checking the client IP itself against
    the tighter per-user threshold, rather than skipping the per-user
    tier outright. Without this, sending no (or no parseable)
    anonymous_id would dodge the tight tier entirely and leave only the
    much more generous IP backstop in effect, exactly the gap the
    per-user tier exists to close."""
    ip = _client_ip(request)
    _chat_ip_limiter.check(ip)
    try:
        body = await request.json()
    except Exception:
        body = None
    anonymous_id = body.get("anonymous_id") if isinstance(body, dict) else None
    valid = isinstance(anonymous_id, str) and 0 < len(anonymous_id) <= MAX_KEY_LENGTH
    _user_limiter.check(anonymous_id if valid else ip)


def reset_for_tests() -> None:
    """Clears all in-memory rate-limit state in all three limiters.
    Module-level state means every test hitting /chat or /trip/plan
    through the same TestClient shares the same buckets -- without a
    reset between tests, an unrelated test late in a run could start
    getting 429s purely from earlier tests' request counts. Call this
    from an autouse fixture in any test file that exercises those
    endpoints."""
    _trip_plan_limiter.reset_for_tests()
    _chat_ip_limiter.reset_for_tests()
    _user_limiter.reset_for_tests()
