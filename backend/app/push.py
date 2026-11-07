"""backend/app/push.py

Web Push (RFC 8030, VAPID-authenticated) -- sends real-time disruption/
reroute alerts straight to a rider's device without them needing /chat
open, via `pywebpush`, the standard Python implementation of the Web
Push protocol (the library essentially every Python Web Push tutorial
uses; no reason to hand-roll VAPID JWT signing or the aes128gcm payload
encryption it already does correctly).

Layered on top of, not replacing, the existing pull-based
`pending_notification` path (see `app/trip_monitor.py`/
`app/agents/conversation_agent.py`): a push is best-effort and silent on
failure (no subscription, permission revoked, browser offline) -- the
rider still sees the same notification text the next time they open
/chat regardless of whether the push itself was ever delivered.
"""
import json
import logging
import sqlite3
from datetime import datetime, timezone

from pywebpush import WebPushException, webpush

from app.config import settings
from db import get_connection

# `sub` just tells a push service who to contact about this app's use of
# their service if something goes wrong (Apple/Google/Mozilla's push
# infra, not this product's own users) -- a generic project contact, not
# any individual's personal email address.
VAPID_CLAIMS = {"sub": "mailto:notifications@is-my-train-screwed.app"}


def save_subscription(
    anonymous_id: str, subscription: dict, conn: sqlite3.Connection | None = None
) -> None:
    """Upserts a browser's PushSubscription by its endpoint (the stable
    identity for "this specific subscription" -- see db.py's schema
    comment). A rider re-subscribing (e.g. after clearing site data)
    just overwrites the same row rather than accumulating duplicates."""
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO push_subscriptions (anonymous_id, endpoint, subscription_json, created_at)
            VALUES (:anonymous_id, :endpoint, :subscription_json, :created_at)
            ON CONFLICT (endpoint) DO UPDATE SET
                anonymous_id = excluded.anonymous_id,
                subscription_json = excluded.subscription_json
            """,
            {
                "anonymous_id": anonymous_id,
                "endpoint": subscription["endpoint"],
                "subscription_json": json.dumps(subscription),
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        conn.commit()
    finally:
        if owns_conn:
            conn.close()


def send_push(
    anonymous_id: str, title: str, body: str, conn: sqlite3.Connection | None = None
) -> None:
    """Best-effort push to every subscription on file for `anonymous_id`.
    Never raises: a push failure must not break the pull-based /chat
    notification path this sits on top of. A dead subscription (404/410,
    the standard push-service response once a rider revokes permission
    or uninstalls) is deleted so it's never retried again; any other
    failure is logged and skipped."""
    if not settings.vapid_private_key:
        return  # not configured (e.g. local dev/tests) -- nothing to do
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, subscription_json FROM push_subscriptions WHERE anonymous_id = ?",
            (anonymous_id,),
        ).fetchall()
        payload = json.dumps({"title": title, "body": body})
        for row in rows:
            try:
                webpush(
                    subscription_info=json.loads(row["subscription_json"]),
                    data=payload,
                    vapid_private_key=settings.vapid_private_key,
                    vapid_claims=dict(VAPID_CLAIMS),
                )
            except WebPushException as exc:
                status = getattr(exc.response, "status_code", None)
                if status in (404, 410):
                    conn.execute("DELETE FROM push_subscriptions WHERE id = ?", (row["id"],))
                    conn.commit()
                else:
                    logging.exception(f"push: delivery failed for subscription {row['id']}")
            except Exception:
                # Caught live on review: pywebpush's webpush() calls
                # `requests.post(...)` with no try/except of its own
                # (confirmed by reading WebPusher.send's source) -- a
                # transient network failure (timeout, DNS, connection
                # reset) raises a bare `requests.exceptions.*`, NOT a
                # WebPushException, so the except clause above alone
                # doesn't cover it. Broad on purpose: this function's
                # entire contract is "never raises" (see its docstring),
                # and pywebpush doesn't document an exhaustive exception
                # list to catch more narrowly than that.
                logging.exception(f"push: delivery failed for subscription {row['id']}")
    finally:
        if owns_conn:
            conn.close()
