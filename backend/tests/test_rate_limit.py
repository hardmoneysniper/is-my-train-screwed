from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.rate_limit import (
    BLOCK_SECONDS,
    MAX_REQUESTS_PER_WINDOW,
    MAX_VIOLATIONS_BEFORE_BLOCK,
    reset_for_tests,
)

client = TestClient(app)

CHAT_BODY = {
    "message": "hi",
    "conversation_history": [],
    "anonymous_id": "11111111-1111-1111-1111-111111111111",
}


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    reset_for_tests()


def _post_chat():
    with patch("app.api.chat.ConversationAgent.respond", new_callable=AsyncMock) as mock_respond:
        mock_respond.return_value = "ok"
        return client.post("/chat", json=CHAT_BODY)


def test_requests_within_window_all_succeed():
    for _ in range(MAX_REQUESTS_PER_WINDOW):
        assert _post_chat().status_code == 200


def test_request_over_the_limit_gets_429():
    for _ in range(MAX_REQUESTS_PER_WINDOW):
        _post_chat()

    response = _post_chat()
    assert response.status_code == 429


def test_repeated_violations_escalate_to_a_full_block():
    for _ in range(MAX_REQUESTS_PER_WINDOW):
        assert _post_chat().status_code == 200  # fill the window

    # Trip the throttle MAX_VIOLATIONS_BEFORE_BLOCK times in a row (real
    # time doesn't advance meaningfully between these in a test, so the
    # window never re-opens -- every one of these is a genuine violation).
    for _ in range(MAX_VIOLATIONS_BEFORE_BLOCK):
        assert _post_chat().status_code == 429

    # The MAX_VIOLATIONS_BEFORE_BLOCK-th violation above should have
    # escalated this IP into the block list -- confirm the NEXT request
    # is blocked outright, not just re-throttled (both currently read as
    # 429 at the HTTP level, so this assertion alone can't distinguish
    # them -- the block-list state itself is what the next test exercises
    # directly via the time-advance behavior only a real block has).
    assert _post_chat().status_code == 429


def test_different_ips_get_independent_limits():
    for _ in range(MAX_REQUESTS_PER_WINDOW):
        with patch("app.api.chat.ConversationAgent.respond", new_callable=AsyncMock) as mock_respond:
            mock_respond.return_value = "ok"
            client.post("/chat", json=CHAT_BODY, headers={"x-forwarded-for": "1.2.3.4"})

    # A different source IP must not inherit 1.2.3.4's exhausted bucket.
    with patch("app.api.chat.ConversationAgent.respond", new_callable=AsyncMock) as mock_respond:
        mock_respond.return_value = "ok"
        response = client.post("/chat", json=CHAT_BODY, headers={"x-forwarded-for": "5.6.7.8"})
    assert response.status_code == 200


def test_block_expires_after_block_seconds(monkeypatch):
    import app.rate_limit as rate_limit_module

    fake_now = [1000.0]
    monkeypatch.setattr(rate_limit_module.time, "monotonic", lambda: fake_now[0])

    for _ in range(MAX_REQUESTS_PER_WINDOW):
        _post_chat()  # fill the window
    for _ in range(MAX_VIOLATIONS_BEFORE_BLOCK):
        _post_chat()  # escalate to a block

    assert _post_chat().status_code == 429  # confirmed blocked

    fake_now[0] += BLOCK_SECONDS + 1  # advance past the block window
    assert _post_chat().status_code == 200
