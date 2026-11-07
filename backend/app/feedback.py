"""backend/app/feedback.py

Post-trip feedback request/response lifecycle. Pure functions, no LLM
calls here -- same discipline as `monitoring.py`/`risk_engine.py`/
`deadline.py`. The quality-check + categorize/dedupe pipeline that turns
a captured raw response into something triaged lives in
`app/agents/feedback_triage_agent.py`, which DOES call an LLM (allowed:
classifying/summarizing text is not "computing a number, route, or
probability").

Lifecycle (2026-11-06, user's explicit design):
1. `request_feedback` queues a request the moment a monitored trip ends
   (`app/monitoring.cancel_monitored_trip` -- the rider said they arrived
   or asked to stop -- or `app/trip_monitor.py`'s TTL-expiry branch).
2. `claim_unasked_feedback_prompt` delivers it on the rider's NEXT /chat
   reply -- same pull-on-next-turn pattern every other notification in
   this product already uses (monitoring is pull-only, see CLAUDE.md).
3. `claim_awaiting_feedback_response` then captures whatever the rider
   types in the turn immediately after that delivery as their feedback
   text, UNCONDITIONALLY -- no LLM judgment call needed server-side to
   recognize "this is feedback" vs. "this is a new question". The
   downstream quality-check agent is what catches it if that next
   message was actually unrelated (the rider ignored the prompt and just
   asked something new) rather than genuine feedback.
"""
import sqlite3
from datetime import datetime, timezone

from db import get_connection


def request_feedback(trip_id: int, anonymous_id: str, conn: sqlite3.Connection | None = None) -> None:
    """Queues a feedback request for a just-ended trip. INSERT OR IGNORE
    against the trip_id unique index -- safe to call more than once for
    the same trip (e.g. a retried caller) without queuing a duplicate ask."""
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO feedback (trip_id, anonymous_id, requested_at) VALUES (?, ?, ?)",
            (trip_id, anonymous_id, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        if owns_conn:
            conn.close()


def claim_unasked_feedback_prompt(conn: sqlite3.Connection, anonymous_id: str) -> dict | None:
    """Atomically claims (sets asked_at) the oldest not-yet-delivered
    feedback request for `anonymous_id`, returning it, or `None` if there
    isn't one. One `UPDATE ... RETURNING`, same atomicity reasoning as
    `db.claim_pending_notifications` -- guards against two concurrent
    /chat calls for the same rider both delivering the same ask twice."""
    cursor = conn.execute(
        """
        UPDATE feedback SET asked_at = ?
        WHERE id = (
            SELECT id FROM feedback
            WHERE anonymous_id = ? AND asked_at IS NULL
            ORDER BY requested_at ASC LIMIT 1
        )
        RETURNING *
        """,
        (datetime.now(timezone.utc).isoformat(), anonymous_id),
    )
    row = cursor.fetchone()
    conn.commit()
    return dict(row) if row is not None else None


def claim_awaiting_feedback_response(
    conn: sqlite3.Connection, anonymous_id: str, raw_text: str
) -> dict | None:
    """Atomically claims (sets responded_at + raw_text) the feedback
    request `anonymous_id` is currently awaiting a response to -- the
    oldest row with `asked_at` set and `responded_at` still NULL. Returns
    `None` if there's nothing awaiting a response, meaning the caller's
    message is a normal chat turn, not a feedback capture."""
    cursor = conn.execute(
        """
        UPDATE feedback SET responded_at = ?, raw_text = ?
        WHERE id = (
            SELECT id FROM feedback
            WHERE anonymous_id = ? AND asked_at IS NOT NULL AND responded_at IS NULL
            ORDER BY asked_at ASC LIMIT 1
        )
        RETURNING *
        """,
        (datetime.now(timezone.utc).isoformat(), raw_text, anonymous_id),
    )
    row = cursor.fetchone()
    conn.commit()
    return dict(row) if row is not None else None
