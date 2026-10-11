from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app import otp_lifecycle


@pytest.fixture(autouse=True)
def _reset():
    otp_lifecycle.reset_for_tests()


def _ok_response(json_body=None):
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=json_body or {})
    return resp


@pytest.mark.asyncio
async def test_is_otp_reachable_true_on_200():
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=_ok_response()):
        assert await otp_lifecycle.is_otp_reachable() is True


@pytest.mark.asyncio
async def test_is_otp_reachable_false_on_connect_error():
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=httpx.ConnectError("refused")):
        assert await otp_lifecycle.is_otp_reachable() is False


@pytest.mark.asyncio
async def test_ensure_otp_awake_returns_true_immediately_when_already_reachable():
    with patch("app.otp_lifecycle.is_otp_reachable", new_callable=AsyncMock, return_value=True), \
         patch("app.otp_lifecycle.wake_otp", new_callable=AsyncMock) as mock_wake:
        assert await otp_lifecycle.ensure_otp_awake() is True
    mock_wake.assert_not_called()  # already up -- never triggers a wake


@pytest.mark.asyncio
async def test_ensure_otp_awake_wakes_and_waits_until_reachable():
    # Unreachable the first two checks, reachable the third -- proves it
    # actually polls rather than giving up after one failed check.
    with patch("app.otp_lifecycle.is_otp_reachable", new_callable=AsyncMock, side_effect=[False, False, True]), \
         patch("app.otp_lifecycle.wake_otp", new_callable=AsyncMock) as mock_wake, \
         patch("asyncio.sleep", new_callable=AsyncMock):
        assert await otp_lifecycle.ensure_otp_awake() is True
    mock_wake.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_otp_awake_returns_false_when_still_down_after_budget():
    # A fake clock that advances by 10s on every monotonic() call, forever
    # -- independent of exactly how many calls ensure_otp_awake() makes or
    # what ENSURE_AWAKE_MAX_WAIT_SECONDS is currently set to, this must
    # eventually exceed the budget and the loop must stop rather than spin.
    fake_now = [0.0]

    def _advancing_clock():
        fake_now[0] += 10.0
        return fake_now[0]

    with patch("app.otp_lifecycle.is_otp_reachable", new_callable=AsyncMock, return_value=False), \
         patch("app.otp_lifecycle.wake_otp", new_callable=AsyncMock), \
         patch("asyncio.sleep", new_callable=AsyncMock), \
         patch("time.monotonic", side_effect=_advancing_clock):
        assert await otp_lifecycle.ensure_otp_awake() is False


def test_mark_otp_used_sets_last_used_timestamp():
    assert otp_lifecycle._last_used_at is None
    otp_lifecycle.mark_otp_used()
    assert otp_lifecycle._last_used_at is not None


@pytest.mark.asyncio
async def test_stop_otp_if_idle_noop_when_never_used():
    with patch("app.otp_lifecycle._railway_graphql", new_callable=AsyncMock) as mock_gql:
        await otp_lifecycle.stop_otp_if_idle()
    mock_gql.assert_not_called()


@pytest.mark.asyncio
async def test_stop_otp_if_idle_noop_when_recently_used():
    otp_lifecycle.mark_otp_used()
    with patch("app.otp_lifecycle._railway_graphql", new_callable=AsyncMock) as mock_gql:
        await otp_lifecycle.stop_otp_if_idle()
    mock_gql.assert_not_called()


@pytest.mark.asyncio
async def test_stop_otp_if_idle_stops_when_idle_and_running(monkeypatch):
    otp_lifecycle.mark_otp_used()
    # Jump "now" far past IDLE_STOP_AFTER_SECONDS without real sleeping.
    monkeypatch.setattr(
        otp_lifecycle.time, "monotonic",
        lambda: otp_lifecycle._last_used_at + otp_lifecycle.IDLE_STOP_AFTER_SECONDS + 1,
    )
    with patch("app.otp_lifecycle.is_otp_reachable", new_callable=AsyncMock, return_value=True), \
         patch("app.otp_lifecycle._current_otp_deployment_id", new_callable=AsyncMock, return_value="dep-1"), \
         patch("app.otp_lifecycle._railway_graphql", new_callable=AsyncMock) as mock_gql:
        await otp_lifecycle.stop_otp_if_idle()
    mock_gql.assert_awaited_once()
    assert "dep-1" in mock_gql.call_args.args[0]
    assert "deploymentStop" in mock_gql.call_args.args[0]


@pytest.mark.asyncio
async def test_stop_otp_if_idle_skips_when_already_stopped(monkeypatch):
    otp_lifecycle.mark_otp_used()
    monkeypatch.setattr(
        otp_lifecycle.time, "monotonic",
        lambda: otp_lifecycle._last_used_at + otp_lifecycle.IDLE_STOP_AFTER_SECONDS + 1,
    )
    with patch("app.otp_lifecycle.is_otp_reachable", new_callable=AsyncMock, return_value=False), \
         patch("app.otp_lifecycle._railway_graphql", new_callable=AsyncMock) as mock_gql:
        await otp_lifecycle.stop_otp_if_idle()
    mock_gql.assert_not_called()  # nothing to stop -- already down
