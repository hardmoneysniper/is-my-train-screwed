import json
from unittest.mock import MagicMock, patch

import pytest
from pywebpush import WebPushException

from app.push import save_subscription, send_push
from db import get_connection

SUBSCRIPTION = {
    "endpoint": "https://fcm.googleapis.com/fcm/send/abc123",
    "keys": {"p256dh": "key1", "auth": "key2"},
}


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(str(tmp_path / "push.sqlite3"))
    yield connection
    connection.close()


def _fetch_all(conn):
    return conn.execute("SELECT * FROM push_subscriptions").fetchall()


# --- save_subscription --------------------------------------------------------


def test_save_subscription_inserts_a_row(conn):
    save_subscription("anon-1", SUBSCRIPTION, conn=conn)

    rows = _fetch_all(conn)
    assert len(rows) == 1
    assert rows[0]["anonymous_id"] == "anon-1"
    assert rows[0]["endpoint"] == SUBSCRIPTION["endpoint"]
    assert json.loads(rows[0]["subscription_json"]) == SUBSCRIPTION


def test_save_subscription_upserts_by_endpoint_not_duplicating(conn):
    save_subscription("anon-1", SUBSCRIPTION, conn=conn)
    save_subscription("anon-1", SUBSCRIPTION, conn=conn)

    assert len(_fetch_all(conn)) == 1


def test_save_subscription_reassigns_anonymous_id_on_resubscribe(conn):
    # A rider re-subscribing from the same browser (e.g. after clearing
    # site data and getting a new anonymous_id) should take over the row
    # rather than leaving it pointed at the old, now-orphaned id.
    save_subscription("anon-old", SUBSCRIPTION, conn=conn)
    save_subscription("anon-new", SUBSCRIPTION, conn=conn)

    rows = _fetch_all(conn)
    assert len(rows) == 1
    assert rows[0]["anonymous_id"] == "anon-new"


# --- send_push -----------------------------------------------------------------


def test_send_push_noops_without_a_configured_vapid_key(conn):
    save_subscription("anon-1", SUBSCRIPTION, conn=conn)
    with patch("app.push.settings.vapid_private_key", ""), patch("app.push.webpush") as mock_webpush:
        send_push("anon-1", "Trip update", "rerouted", conn=conn)

    mock_webpush.assert_not_called()


def test_send_push_calls_webpush_for_every_subscription(conn):
    save_subscription("anon-1", SUBSCRIPTION, conn=conn)
    with patch("app.push.settings.vapid_private_key", "fake-key"), patch("app.push.webpush") as mock_webpush:
        send_push("anon-1", "Trip update", "rerouted via Q train", conn=conn)

    mock_webpush.assert_called_once()
    kwargs = mock_webpush.call_args.kwargs
    assert kwargs["subscription_info"] == SUBSCRIPTION
    assert json.loads(kwargs["data"]) == {"title": "Trip update", "body": "rerouted via Q train"}
    assert kwargs["vapid_private_key"] == "fake-key"


def test_send_push_deletes_subscription_on_410_gone(conn):
    save_subscription("anon-1", SUBSCRIPTION, conn=conn)
    exc = WebPushException("gone")
    exc.response = MagicMock(status_code=410)
    with patch("app.push.settings.vapid_private_key", "fake-key"), patch("app.push.webpush", side_effect=exc):
        send_push("anon-1", "Trip update", "rerouted", conn=conn)

    assert _fetch_all(conn) == []


def test_send_push_keeps_subscription_on_a_non_gone_failure(conn):
    save_subscription("anon-1", SUBSCRIPTION, conn=conn)
    exc = WebPushException("server error")
    exc.response = MagicMock(status_code=500)
    with patch("app.push.settings.vapid_private_key", "fake-key"), patch("app.push.webpush", side_effect=exc):
        send_push("anon-1", "Trip update", "rerouted", conn=conn)  # must not raise

    assert len(_fetch_all(conn)) == 1


def test_send_push_swallows_a_raw_network_error_not_just_webpushexception(conn):
    # Regression test, caught on review: pywebpush's webpush() calls
    # requests.post(...) with no try/except of its own -- a transient
    # network failure (timeout, DNS, connection reset) raises a bare
    # requests exception, not a WebPushException. send_push's own
    # docstring promises "never raises"; this must hold for that case too.
    save_subscription("anon-1", SUBSCRIPTION, conn=conn)
    with patch("app.push.settings.vapid_private_key", "fake-key"), \
         patch("app.push.webpush", side_effect=ConnectionError("network unreachable")):
        send_push("anon-1", "Trip update", "rerouted", conn=conn)  # must not raise

    assert len(_fetch_all(conn)) == 1  # not a 404/410 -- subscription is kept, not deleted


def test_send_push_continues_to_other_subscriptions_after_one_fails(conn):
    other_subscription = {**SUBSCRIPTION, "endpoint": "https://fcm.googleapis.com/fcm/send/other"}
    save_subscription("anon-1", SUBSCRIPTION, conn=conn)
    save_subscription("anon-1", other_subscription, conn=conn)
    with patch("app.push.settings.vapid_private_key", "fake-key"), \
         patch("app.push.webpush", side_effect=[ConnectionError("boom"), None]) as mock_webpush:
        send_push("anon-1", "Trip update", "rerouted", conn=conn)  # must not raise

    assert mock_webpush.call_count == 2  # the second subscription was still attempted


def test_send_push_with_no_subscriptions_does_nothing(conn):
    with patch("app.push.settings.vapid_private_key", "fake-key"), patch("app.push.webpush") as mock_webpush:
        send_push("anon-nobody", "Trip update", "rerouted", conn=conn)  # must not raise

    mock_webpush.assert_not_called()
