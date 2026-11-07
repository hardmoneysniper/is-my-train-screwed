import pytest

from app.feedback import (
    claim_awaiting_feedback_response,
    claim_unasked_feedback_prompt,
    request_feedback,
)
from db import get_connection


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(str(tmp_path / "feedback.sqlite3"))
    yield connection
    connection.close()


def _fetch(conn, feedback_id):
    return conn.execute("SELECT * FROM feedback WHERE id = ?", (feedback_id,)).fetchone()


# --- request_feedback --------------------------------------------------------


def test_request_feedback_inserts_a_row(conn):
    request_feedback(1, "anon-1", conn=conn)

    row = conn.execute("SELECT * FROM feedback WHERE trip_id = 1").fetchone()
    assert row is not None
    assert row["anonymous_id"] == "anon-1"
    assert row["asked_at"] is None
    assert row["responded_at"] is None


def test_request_feedback_twice_for_the_same_trip_does_not_duplicate(conn):
    request_feedback(1, "anon-1", conn=conn)
    request_feedback(1, "anon-1", conn=conn)

    rows = conn.execute("SELECT * FROM feedback WHERE trip_id = 1").fetchall()
    assert len(rows) == 1


# --- claim_unasked_feedback_prompt -------------------------------------------


def test_claim_unasked_feedback_prompt_returns_none_when_nothing_pending(conn):
    assert claim_unasked_feedback_prompt(conn, "anon-1") is None


def test_claim_unasked_feedback_prompt_claims_and_sets_asked_at(conn):
    request_feedback(1, "anon-1", conn=conn)

    claimed = claim_unasked_feedback_prompt(conn, "anon-1")

    assert claimed is not None
    assert claimed["trip_id"] == 1
    row = _fetch(conn, claimed["id"])
    assert row["asked_at"] is not None


def test_claim_unasked_feedback_prompt_is_not_claimed_twice(conn):
    request_feedback(1, "anon-1", conn=conn)
    claim_unasked_feedback_prompt(conn, "anon-1")

    assert claim_unasked_feedback_prompt(conn, "anon-1") is None


def test_claim_unasked_feedback_prompt_only_claims_for_the_right_anonymous_id(conn):
    request_feedback(1, "anon-1", conn=conn)

    assert claim_unasked_feedback_prompt(conn, "anon-2") is None


def test_claim_unasked_feedback_prompt_picks_the_oldest_request_first(conn):
    request_feedback(1, "anon-1", conn=conn)
    request_feedback(2, "anon-1", conn=conn)

    first_claimed = claim_unasked_feedback_prompt(conn, "anon-1")
    assert first_claimed["trip_id"] == 1

    second_claimed = claim_unasked_feedback_prompt(conn, "anon-1")
    assert second_claimed["trip_id"] == 2


# --- claim_awaiting_feedback_response ----------------------------------------


def test_claim_awaiting_feedback_response_returns_none_when_nothing_was_asked(conn):
    request_feedback(1, "anon-1", conn=conn)  # queued but never delivered yet

    assert claim_awaiting_feedback_response(conn, "anon-1", "it was fine") is None


def test_claim_awaiting_feedback_response_captures_text_and_sets_responded_at(conn):
    request_feedback(1, "anon-1", conn=conn)
    claimed_prompt = claim_unasked_feedback_prompt(conn, "anon-1")

    captured = claim_awaiting_feedback_response(conn, "anon-1", "the bus was late but ok otherwise")

    assert captured is not None
    assert captured["id"] == claimed_prompt["id"]
    assert captured["raw_text"] == "the bus was late but ok otherwise"
    row = _fetch(conn, claimed_prompt["id"])
    assert row["responded_at"] is not None


def test_claim_awaiting_feedback_response_is_not_claimed_twice(conn):
    request_feedback(1, "anon-1", conn=conn)
    claim_unasked_feedback_prompt(conn, "anon-1")
    claim_awaiting_feedback_response(conn, "anon-1", "first reply")

    # A second message right after must be treated as a normal chat turn,
    # not a second feedback capture -- there's nothing left awaiting.
    assert claim_awaiting_feedback_response(conn, "anon-1", "second message") is None


def test_claim_awaiting_feedback_response_only_claims_for_the_right_anonymous_id(conn):
    request_feedback(1, "anon-1", conn=conn)
    claim_unasked_feedback_prompt(conn, "anon-1")

    assert claim_awaiting_feedback_response(conn, "anon-2", "not mine") is None
