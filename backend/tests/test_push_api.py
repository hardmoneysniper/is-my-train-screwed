from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

SUBSCRIPTION = {
    "endpoint": "https://fcm.googleapis.com/fcm/send/abc123",
    "keys": {"p256dh": "key1", "auth": "key2"},
}


def test_public_key_returns_the_configured_vapid_public_key():
    with patch("app.api.push.settings.vapid_public_key", "fake-public-key"):
        response = client.get("/push/public-key")

    assert response.status_code == 200
    assert response.json() == {"public_key": "fake-public-key"}


def test_subscribe_saves_the_subscription():
    with patch("app.api.push.save_subscription") as mock_save:
        response = client.post("/push/subscribe", json={
            "anonymous_id": "anon-1",
            "subscription": SUBSCRIPTION,
        })

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    mock_save.assert_called_once_with("anon-1", SUBSCRIPTION)


def test_subscribe_rejects_a_missing_subscription_field():
    response = client.post("/push/subscribe", json={"anonymous_id": "anon-1"})
    assert response.status_code == 422


def test_subscribe_rejects_a_subscription_missing_endpoint():
    # Regression test: previously `subscription` was a bare dict, so a
    # missing "endpoint" key would reach save_subscription and raise an
    # unhandled KeyError (-> 500) instead of a clean 422.
    response = client.post("/push/subscribe", json={
        "anonymous_id": "anon-1",
        "subscription": {"keys": {"p256dh": "key1", "auth": "key2"}},
    })
    assert response.status_code == 422


def test_subscribe_passes_through_extra_subscription_fields():
    with patch("app.api.push.save_subscription") as mock_save:
        response = client.post("/push/subscribe", json={
            "anonymous_id": "anon-1",
            "subscription": {**SUBSCRIPTION, "expirationTime": None},
        })

    assert response.status_code == 200
    mock_save.assert_called_once_with("anon-1", {**SUBSCRIPTION, "expirationTime": None})
