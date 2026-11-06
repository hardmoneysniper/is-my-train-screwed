from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
import app.rate_limit as rate_limit_module
from app.rate_limit import (
    BLOCK_SECONDS,
    CHAT_IP_MAX_REQUESTS,
    MAX_TRACKED_KEYS,
    MAX_VIOLATIONS_BEFORE_BLOCK,
    TRIP_PLAN_MAX_REQUESTS,
    USER_MAX_REQUESTS,
    _Limiter,
    reset_for_tests,
)

client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    reset_for_tests()


def _chat_body(anonymous_id="11111111-1111-1111-1111-111111111111"):
    return {"message": "hi", "conversation_history": [], "anonymous_id": anonymous_id}


def _post_chat(anonymous_id="11111111-1111-1111-1111-111111111111", **kwargs):
    with patch("app.api.chat.ConversationAgent.respond", new_callable=AsyncMock) as mock_respond:
        mock_respond.return_value = "ok"
        return client.post("/chat", json=_chat_body(anonymous_id), **kwargs)


def test_requests_within_both_limits_all_succeed():
    for _ in range(USER_MAX_REQUESTS):
        assert _post_chat().status_code == 200


def test_request_over_the_user_limit_gets_429():
    # USER_MAX_REQUESTS is the tighter of the two tiers, so it's the one
    # a single anonymous_id hits first.
    for _ in range(USER_MAX_REQUESTS):
        _post_chat()

    assert _post_chat().status_code == 429


def test_request_over_the_chat_ip_limit_gets_429_even_with_different_anonymous_ids():
    # Rotating anonymous_id evades the per-user tier but must still trip
    # the IP backstop -- otherwise the IP tier would be pointless.
    for i in range(CHAT_IP_MAX_REQUESTS):
        _post_chat(anonymous_id=f"id-{i}")

    assert _post_chat(anonymous_id="id-final").status_code == 429


def test_different_ips_with_different_users_get_independent_limits():
    for _ in range(USER_MAX_REQUESTS):
        _post_chat(anonymous_id="user-a", headers={"x-forwarded-for": "1.2.3.4"})

    # A different source IP AND a different real user must not inherit
    # 1.2.3.4/user-a's exhausted buckets.
    response = _post_chat(anonymous_id="user-b", headers={"x-forwarded-for": "5.6.7.8"})
    assert response.status_code == 200


def test_same_anonymous_id_from_a_different_ip_shares_the_user_bucket():
    # Documents intended behavior, not a regression: _user_limiter keys
    # purely on anonymous_id, with no IP scoping -- a real user who
    # switches networks (wifi to cellular) mid-session keeps the SAME
    # per-user quota rather than getting a fresh one just by changing IP.
    for _ in range(USER_MAX_REQUESTS):
        assert _post_chat(anonymous_id="user-a", headers={"x-forwarded-for": "1.1.1.1"}).status_code == 200
    assert _post_chat(anonymous_id="user-a", headers={"x-forwarded-for": "2.2.2.2"}).status_code == 429


def test_same_ip_different_users_each_get_their_own_user_bucket():
    # The scenario this split was built for (2026-11-06): a shared IP
    # (e.g. one campus network) with multiple real users, each within
    # their own per-user limit and well under the IP backstop, must all
    # succeed rather than sharing one bucket.
    for _ in range(USER_MAX_REQUESTS):
        assert _post_chat(anonymous_id="user-a", headers={"x-forwarded-for": "9.9.9.9"}).status_code == 200
    for _ in range(USER_MAX_REQUESTS):
        assert _post_chat(anonymous_id="user-b", headers={"x-forwarded-for": "9.9.9.9"}).status_code == 200


def test_repeated_user_violations_escalate_to_a_full_block():
    for _ in range(USER_MAX_REQUESTS):
        assert _post_chat().status_code == 200  # fill the window

    # Trip the throttle MAX_VIOLATIONS_BEFORE_BLOCK times in a row (real
    # time doesn't advance meaningfully between these in a test, so the
    # window never re-opens -- every one of these is a genuine violation).
    for _ in range(MAX_VIOLATIONS_BEFORE_BLOCK):
        assert _post_chat().status_code == 429

    # The MAX_VIOLATIONS_BEFORE_BLOCK-th violation above should have
    # escalated this anonymous_id into the block list -- confirm the
    # NEXT request is blocked outright, not just re-throttled (both
    # currently read as 429 at the HTTP level, so this assertion alone
    # can't distinguish them -- the block-list state itself is what the
    # next test exercises directly via the time-advance behavior only a
    # real block has).
    assert _post_chat().status_code == 429


def test_block_expires_after_block_seconds(monkeypatch):
    import app.rate_limit as rate_limit_module

    fake_now = [1000.0]
    monkeypatch.setattr(rate_limit_module.time, "monotonic", lambda: fake_now[0])

    for _ in range(USER_MAX_REQUESTS):
        _post_chat()  # fill the window
    for _ in range(MAX_VIOLATIONS_BEFORE_BLOCK):
        _post_chat()  # escalate to a block

    assert _post_chat().status_code == 429  # confirmed blocked

    fake_now[0] += BLOCK_SECONDS + 1  # advance past the block window
    assert _post_chat().status_code == 200


def test_trip_plan_keeps_its_own_lower_ip_limit_independent_of_chat():
    # /trip/plan can trigger a real-cost OTP wake, so it must keep its
    # own original, tighter per-IP ceiling -- not inherit /chat's more
    # generous backstop just because they share the same _Limiter class.
    body = {"from_lat": 40.76, "from_lon": -73.95, "to_lat": 40.75, "to_lon": -73.97}
    with patch("app.api.trip.ensure_otp_awake", new_callable=AsyncMock, return_value=False):
        for _ in range(TRIP_PLAN_MAX_REQUESTS):
            assert client.post("/trip/plan", json=body).status_code == 503  # otp starting, not rate-limited
        assert client.post("/trip/plan", json=body).status_code == 429

    # Using up /trip/plan's bucket from this IP must not touch /chat's
    # separate IP bucket -- confirms the two are genuinely independent
    # limiter instances, not the same one shared across endpoints.
    assert _post_chat().status_code == 200


def test_chat_with_malformed_body_does_not_500():
    # enforce_chat_rate_limit must not itself crash on bad input -- that
    # would turn FastAPI's normal clean 422 for a malformed body into an
    # unhandled 500 raised from the rate limiter instead of real
    # request validation.
    response = client.post(
        "/chat", content=b"not json", headers={"content-type": "application/json"}
    )
    assert response.status_code == 422


def test_chat_with_no_anonymous_id_field_still_applies_the_user_tier_via_ip_fallback():
    # A body that's valid JSON but omits anonymous_id entirely (unlike
    # truly malformed/non-JSON bodies -- confirmed separately to never
    # even reach this dependency, since FastAPI's own body decoding
    # rejects those before any dependency runs, at zero cost either way)
    # must not be a way to dodge the per-user tier -- it should fall
    # back to the IP, same as an empty/oversized anonymous_id.
    for _ in range(USER_MAX_REQUESTS):
        response = client.post("/chat", json={"message": "hi", "conversation_history": []})
        assert response.status_code == 422  # still missing the required anonymous_id field
    response = client.post("/chat", json={"message": "hi", "conversation_history": []})
    assert response.status_code == 429  # user tier (IP-keyed fallback) now trips first


def test_overlong_anonymous_id_falls_back_to_ip_on_the_user_tier():
    # An oversized anonymous_id must not be trackable as its own key
    # (unbounded key-space growth risk), but it must still fall back to
    # the IP-keyed check on the (tighter) user tier rather than silently
    # skipping per-user throttling altogether.
    huge_id = "x" * 500
    for _ in range(USER_MAX_REQUESTS):
        assert _post_chat(anonymous_id=huge_id).status_code == 200
    assert _post_chat(anonymous_id=huge_id).status_code == 429


def test_empty_anonymous_id_still_gets_throttled_via_ip_fallback():
    # Sending no real anonymous_id must not be a way to dodge the tight
    # per-user tier and get only the generous IP backstop.
    for _ in range(USER_MAX_REQUESTS):
        assert _post_chat(anonymous_id="").status_code == 200
    assert _post_chat(anonymous_id="").status_code == 429


def test_overflow_eviction_never_erases_an_active_block():
    # Regression test: churning many distinct keys past MAX_TRACKED_KEYS
    # must evict idle entries, never a key that's currently blocked --
    # otherwise an attacker could spoof a fresh key on every request to
    # erase anyone's active block, defeating the block tier entirely.
    limiter = _Limiter(
        window_seconds=60, max_requests=1,
        violation_window_seconds=600, max_violations=1, block_seconds=600,
    )
    with pytest.raises(rate_limit_module.HTTPException):
        limiter.check("blocked-key")  # 1st call ok
        limiter.check("blocked-key")  # 2nd call trips throttle -> 1 violation -> blocked
    assert "blocked-key" in limiter._blocked_until

    for i in range(MAX_TRACKED_KEYS + 10):
        try:
            limiter.check(f"filler-{i}")
        except rate_limit_module.HTTPException:
            pass

    assert "blocked-key" in limiter._blocked_until  # still blocked, not evicted


def test_overflow_eviction_only_drops_one_key_not_everyone():
    # Regression test: at capacity, a new key must free room by evicting
    # exactly one idle tracked key, not every tracked key at once -- a
    # full wipe on every single overflow would reset a huge number of
    # real users' throttle state just because one more user showed up.
    limiter = _Limiter(
        window_seconds=60, max_requests=10,
        violation_window_seconds=600, max_violations=3, block_seconds=600,
    )
    for i in range(MAX_TRACKED_KEYS):
        limiter.check(f"existing-{i}")  # each makes exactly 1 (unthrottled) request

    limiter.check("newcomer")  # triggers overflow eviction for exactly one slot

    survivors = sum(1 for i in range(MAX_TRACKED_KEYS) if f"existing-{i}" in limiter._requests)
    assert survivors == MAX_TRACKED_KEYS - 1  # exactly one evicted, not all of them
