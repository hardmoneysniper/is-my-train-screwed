import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.feedback_triage_agent import triage_feedback
from db import get_connection


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(str(tmp_path / "triage.sqlite3"))
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def no_github_calls_by_default():
    # Real bug, caught live (2026-11-07): without this, every test in this
    # file that reaches genuine/triaged feedback makes a REAL httpx call to
    # api.github.com using this machine's real .env GITHUB_TOKEN/
    # GITHUB_REPO_URL -- which is exactly how two real throwaway issues
    # ended up opened on the actual repo from test fixture text before this
    # fixture existed. Default both to "did nothing" (None/False, same
    # shape as "GitHub not configured") for every test; tests that DO
    # exercise the GitHub wiring override these explicitly within their
    # own `with patch(...)` block.
    with (
        patch("app.agents.feedback_triage_agent.create_issue", new_callable=AsyncMock, return_value=None),
        patch("app.agents.feedback_triage_agent.comment_on_issue", new_callable=AsyncMock, return_value=False),
    ):
        yield


def _insert_feedback(conn, raw_text="the bus was 10 minutes late and the app didn't warn me", trip_id=1) -> int:
    cursor = conn.execute(
        "INSERT INTO feedback (trip_id, anonymous_id, requested_at, asked_at, responded_at, raw_text) "
        "VALUES (?, 'anon-1', '2026-11-06T00:00:00+00:00', '2026-11-06T00:01:00+00:00', "
        "'2026-11-06T00:02:00+00:00', ?)",
        (trip_id, raw_text),
    )
    conn.commit()
    return cursor.lastrowid


def _mark_genuine(conn, feedback_id, summary, github_issue_number=None):
    conn.execute(
        "UPDATE feedback SET quality_status='genuine', summary=?, github_issue_number=?, "
        "triaged_at='2026-11-05T00:00:00+00:00' WHERE id=?",
        (summary, github_issue_number, feedback_id),
    )
    conn.commit()


def _fake_usage():
    return MagicMock(model_dump=lambda: {"input_tokens": 50, "output_tokens": 10})


def _text_response(text: str):
    return MagicMock(content=[MagicMock(type="text", text=text)], usage=_fake_usage())


def _quality_response(is_feedback: bool = True):
    return _text_response(json.dumps({"is_feedback": is_feedback}))


def _triage_response(category="bug", summary="A summary.", duplicate_of=None):
    return _text_response(json.dumps({"category": category, "summary": summary, "duplicate_of": duplicate_of}))


def _fetch(conn, feedback_id):
    return conn.execute("SELECT * FROM feedback WHERE id = ?", (feedback_id,)).fetchone()


# --- quality check ------------------------------------------------------------


@pytest.mark.asyncio
async def test_rejected_feedback_is_marked_and_not_categorized(conn):
    feedback_id = _insert_feedback(conn, raw_text="what time is the next F train")
    with (
        patch("app.agents.feedback_triage_agent.AsyncAnthropic") as mock_cls,
        patch("app.agents.feedback_triage_agent.cost_guard.log_call"),
    ):
        mock_cls.return_value.messages.create = AsyncMock(return_value=_quality_response(False))
        await triage_feedback(feedback_id, conn=conn)

    row = _fetch(conn, feedback_id)
    assert row["quality_status"] == "rejected"
    assert row["category"] is None
    assert row["triaged_at"] is not None
    mock_cls.return_value.messages.create.assert_awaited_once()  # only the quality check ran


@pytest.mark.asyncio
async def test_noop_when_feedback_row_has_no_raw_text(conn):
    cursor = conn.execute(
        "INSERT INTO feedback (trip_id, anonymous_id, requested_at) VALUES (1, 'anon-1', '2026-11-06T00:00:00+00:00')"
    )
    conn.commit()
    feedback_id = cursor.lastrowid

    with patch("app.agents.feedback_triage_agent.AsyncAnthropic") as mock_cls:
        await triage_feedback(feedback_id, conn=conn)

    mock_cls.return_value.messages.create.assert_not_called()
    assert _fetch(conn, feedback_id)["quality_status"] is None


@pytest.mark.asyncio
async def test_noop_for_a_nonexistent_feedback_id(conn):
    with patch("app.agents.feedback_triage_agent.AsyncAnthropic") as mock_cls:
        await triage_feedback(999999, conn=conn)  # must not raise

    mock_cls.return_value.messages.create.assert_not_called()


@pytest.mark.asyncio
async def test_malformed_model_response_raises_for_the_caller_to_handle(conn):
    feedback_id = _insert_feedback(conn)
    with (
        patch("app.agents.feedback_triage_agent.AsyncAnthropic") as mock_cls,
        patch("app.agents.feedback_triage_agent.cost_guard.log_call"),
    ):
        mock_cls.return_value.messages.create = AsyncMock(return_value=_text_response("not json"))
        with pytest.raises(ValueError):
            await triage_feedback(feedback_id, conn=conn)


# --- triage: categorization, summary, duplicate detection --------------------


@pytest.mark.asyncio
async def test_genuine_feedback_is_categorized_and_summarized(conn):
    feedback_id = _insert_feedback(conn)
    with (
        patch("app.agents.feedback_triage_agent.AsyncAnthropic") as mock_cls,
        patch("app.agents.feedback_triage_agent.cost_guard.log_call"),
    ):
        mock_cls.return_value.messages.create = AsyncMock(side_effect=[
            _quality_response(True),
            _triage_response(category="bug", summary="App didn't warn about a 10 minute bus delay."),
        ])
        await triage_feedback(feedback_id, conn=conn)

    row = _fetch(conn, feedback_id)
    assert row["quality_status"] == "genuine"
    assert row["category"] == "bug"
    assert row["summary"] == "App didn't warn about a 10 minute bus delay."
    assert row["duplicate_of"] is None


@pytest.mark.asyncio
async def test_duplicate_of_is_recorded_when_the_model_flags_a_real_recent_id(conn):
    earlier_id = _insert_feedback(conn, raw_text="bus was late, no warning")
    _mark_genuine(conn, earlier_id, "App didn't warn about a bus delay.")
    feedback_id = _insert_feedback(conn, raw_text="same thing, the bus delay wasn't flagged", trip_id=2)

    with (
        patch("app.agents.feedback_triage_agent.AsyncAnthropic") as mock_cls,
        patch("app.agents.feedback_triage_agent.cost_guard.log_call"),
    ):
        mock_cls.return_value.messages.create = AsyncMock(side_effect=[
            _quality_response(True),
            _triage_response(summary="Bus delay wasn't flagged.", duplicate_of=earlier_id),
        ])
        await triage_feedback(feedback_id, conn=conn)

    row = _fetch(conn, feedback_id)
    assert row["duplicate_of"] == earlier_id


@pytest.mark.asyncio
async def test_hallucinated_duplicate_of_id_is_rejected_not_stored(conn):
    # Real bug class this guards against: a model naming an id it was
    # never shown (hallucination), or the id of a row that wasn't even
    # eligible to be offered (wrong quality_status) -- storing this blind
    # would create a foreign-key-shaped value pointing at an arbitrary or
    # unrelated row.
    unrelated_id = _insert_feedback(conn, raw_text="unrelated, still pending triage", trip_id=99)
    feedback_id = _insert_feedback(conn, trip_id=2)

    with (
        patch("app.agents.feedback_triage_agent.AsyncAnthropic") as mock_cls,
        patch("app.agents.feedback_triage_agent.cost_guard.log_call"),
    ):
        mock_cls.return_value.messages.create = AsyncMock(side_effect=[
            _quality_response(True),
            _triage_response(duplicate_of=unrelated_id),  # never in `recent` -- not genuine/no summary
        ])
        await triage_feedback(feedback_id, conn=conn)

    assert _fetch(conn, feedback_id)["duplicate_of"] is None


@pytest.mark.asyncio
async def test_non_integer_duplicate_of_is_rejected_not_stored(conn):
    feedback_id = _insert_feedback(conn)
    with (
        patch("app.agents.feedback_triage_agent.AsyncAnthropic") as mock_cls,
        patch("app.agents.feedback_triage_agent.cost_guard.log_call"),
    ):
        mock_cls.return_value.messages.create = AsyncMock(side_effect=[
            _quality_response(True),
            _text_response(json.dumps({"category": "bug", "summary": "x", "duplicate_of": "not-a-number"})),
        ])
        await triage_feedback(feedback_id, conn=conn)

    assert _fetch(conn, feedback_id)["duplicate_of"] is None


@pytest.mark.asyncio
async def test_non_string_category_falls_back_to_other_without_crashing(conn):
    # Regression test: `x in frozenset(...)` requires `x` to be hashable
    # -- a model returning a list/dict for this field must fail
    # validation cleanly, not raise TypeError.
    feedback_id = _insert_feedback(conn)
    with (
        patch("app.agents.feedback_triage_agent.AsyncAnthropic") as mock_cls,
        patch("app.agents.feedback_triage_agent.cost_guard.log_call"),
    ):
        mock_cls.return_value.messages.create = AsyncMock(side_effect=[
            _quality_response(True),
            _text_response(json.dumps({"category": ["bug", "ux"], "summary": "x", "duplicate_of": None})),
        ])
        await triage_feedback(feedback_id, conn=conn)  # must not raise

    assert _fetch(conn, feedback_id)["category"] == "other"


@pytest.mark.asyncio
async def test_unrecognized_category_falls_back_to_other(conn):
    feedback_id = _insert_feedback(conn)
    with (
        patch("app.agents.feedback_triage_agent.AsyncAnthropic") as mock_cls,
        patch("app.agents.feedback_triage_agent.cost_guard.log_call"),
    ):
        mock_cls.return_value.messages.create = AsyncMock(side_effect=[
            _quality_response(True),
            _triage_response(category="totally_invented_category"),
        ])
        await triage_feedback(feedback_id, conn=conn)

    assert _fetch(conn, feedback_id)["category"] == "other"


@pytest.mark.asyncio
async def test_missing_summary_falls_back_to_raw_text(conn):
    feedback_id = _insert_feedback(conn, raw_text="the raw complaint text")
    with (
        patch("app.agents.feedback_triage_agent.AsyncAnthropic") as mock_cls,
        patch("app.agents.feedback_triage_agent.cost_guard.log_call"),
    ):
        mock_cls.return_value.messages.create = AsyncMock(side_effect=[
            _quality_response(True),
            _text_response(json.dumps({"category": "bug", "duplicate_of": None})),  # no "summary" key at all
        ])
        await triage_feedback(feedback_id, conn=conn)

    assert _fetch(conn, feedback_id)["summary"] == "the raw complaint text"


# --- GitHub sync ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_genuine_non_duplicate_opens_a_new_github_issue(conn):
    feedback_id = _insert_feedback(conn, raw_text="the bus was late")
    with (
        patch("app.agents.feedback_triage_agent.AsyncAnthropic") as mock_cls,
        patch("app.agents.feedback_triage_agent.cost_guard.log_call"),
        patch("app.agents.feedback_triage_agent.create_issue", new_callable=AsyncMock, return_value=42) as mock_create,
        patch("app.agents.feedback_triage_agent.comment_on_issue", new_callable=AsyncMock) as mock_comment,
    ):
        mock_cls.return_value.messages.create = AsyncMock(side_effect=[
            _quality_response(True),
            _triage_response(category="bug", summary="The bus was late."),
        ])
        await triage_feedback(feedback_id, conn=conn)

    mock_create.assert_awaited_once()
    args, kwargs = mock_create.call_args
    assert args[0] == "The bus was late."  # title == summary
    assert "the bus was late" in args[1]  # body includes the raw text
    assert kwargs["labels"] == ["bug"]
    mock_comment.assert_not_called()
    assert _fetch(conn, feedback_id)["github_issue_number"] == 42


@pytest.mark.asyncio
async def test_rejected_feedback_never_touches_github(conn):
    feedback_id = _insert_feedback(conn, raw_text="what time is it")
    with (
        patch("app.agents.feedback_triage_agent.AsyncAnthropic") as mock_cls,
        patch("app.agents.feedback_triage_agent.cost_guard.log_call"),
        patch("app.agents.feedback_triage_agent.create_issue", new_callable=AsyncMock) as mock_create,
        patch("app.agents.feedback_triage_agent.comment_on_issue", new_callable=AsyncMock) as mock_comment,
    ):
        mock_cls.return_value.messages.create = AsyncMock(return_value=_quality_response(False))
        await triage_feedback(feedback_id, conn=conn)

    mock_create.assert_not_called()
    mock_comment.assert_not_called()


@pytest.mark.asyncio
async def test_duplicate_with_a_github_issue_comments_instead_of_opening_a_new_one(conn):
    earlier_id = _insert_feedback(conn, raw_text="bus was late, no warning")
    _mark_genuine(conn, earlier_id, "App didn't warn about a bus delay.", github_issue_number=7)
    feedback_id = _insert_feedback(conn, raw_text="same bus delay issue, still not flagged", trip_id=2)

    with (
        patch("app.agents.feedback_triage_agent.AsyncAnthropic") as mock_cls,
        patch("app.agents.feedback_triage_agent.cost_guard.log_call"),
        patch("app.agents.feedback_triage_agent.create_issue", new_callable=AsyncMock) as mock_create,
        patch("app.agents.feedback_triage_agent.comment_on_issue", new_callable=AsyncMock, return_value=True) as mock_comment,
    ):
        mock_cls.return_value.messages.create = AsyncMock(side_effect=[
            _quality_response(True),
            _triage_response(summary="Bus delay wasn't flagged.", duplicate_of=earlier_id),
        ])
        await triage_feedback(feedback_id, conn=conn)

    mock_comment.assert_awaited_once()
    assert mock_comment.call_args.args[0] == 7
    assert "same bus delay issue" in mock_comment.call_args.args[1]
    mock_create.assert_not_called()
    assert _fetch(conn, feedback_id)["github_issue_number"] is None  # doesn't own an issue of its own


@pytest.mark.asyncio
async def test_duplicate_whose_original_has_no_github_issue_opens_a_new_issue_instead(conn):
    # The original's GitHub call may have failed, or GitHub wasn't
    # configured yet when it was triaged -- there's nothing to comment
    # on, so this item must still get its own issue rather than silently
    # losing GitHub coverage.
    earlier_id = _insert_feedback(conn, raw_text="bus was late, no warning")
    _mark_genuine(conn, earlier_id, "App didn't warn about a bus delay.", github_issue_number=None)
    feedback_id = _insert_feedback(conn, raw_text="same bus delay issue again", trip_id=2)

    with (
        patch("app.agents.feedback_triage_agent.AsyncAnthropic") as mock_cls,
        patch("app.agents.feedback_triage_agent.cost_guard.log_call"),
        patch("app.agents.feedback_triage_agent.create_issue", new_callable=AsyncMock, return_value=99) as mock_create,
        patch("app.agents.feedback_triage_agent.comment_on_issue", new_callable=AsyncMock) as mock_comment,
    ):
        mock_cls.return_value.messages.create = AsyncMock(side_effect=[
            _quality_response(True),
            _triage_response(summary="Bus delay wasn't flagged.", duplicate_of=earlier_id),
        ])
        await triage_feedback(feedback_id, conn=conn)

    mock_comment.assert_not_called()
    mock_create.assert_awaited_once()
    assert _fetch(conn, feedback_id)["github_issue_number"] == 99


@pytest.mark.asyncio
async def test_duplicate_whose_comment_fails_falls_back_to_opening_a_new_issue(conn):
    # Regression test: a transient GitHub failure on the comment call
    # must not mean this feedback item gets NO GitHub representation at
    # all -- it should still get its own issue.
    earlier_id = _insert_feedback(conn, raw_text="bus was late, no warning")
    _mark_genuine(conn, earlier_id, "App didn't warn about a bus delay.", github_issue_number=7)
    feedback_id = _insert_feedback(conn, raw_text="same bus delay issue, still not flagged", trip_id=2)

    with (
        patch("app.agents.feedback_triage_agent.AsyncAnthropic") as mock_cls,
        patch("app.agents.feedback_triage_agent.cost_guard.log_call"),
        patch("app.agents.feedback_triage_agent.create_issue", new_callable=AsyncMock, return_value=101) as mock_create,
        patch("app.agents.feedback_triage_agent.comment_on_issue", new_callable=AsyncMock, return_value=False) as mock_comment,
    ):
        mock_cls.return_value.messages.create = AsyncMock(side_effect=[
            _quality_response(True),
            _triage_response(summary="Bus delay wasn't flagged.", duplicate_of=earlier_id),
        ])
        await triage_feedback(feedback_id, conn=conn)

    mock_comment.assert_awaited_once()  # tried the comment first
    mock_create.assert_awaited_once()  # then fell back since it failed
    assert _fetch(conn, feedback_id)["github_issue_number"] == 101


@pytest.mark.asyncio
async def test_github_create_issue_failure_leaves_issue_number_null_without_raising(conn):
    feedback_id = _insert_feedback(conn)
    with (
        patch("app.agents.feedback_triage_agent.AsyncAnthropic") as mock_cls,
        patch("app.agents.feedback_triage_agent.cost_guard.log_call"),
        patch("app.agents.feedback_triage_agent.create_issue", new_callable=AsyncMock, return_value=None),
    ):
        mock_cls.return_value.messages.create = AsyncMock(side_effect=[
            _quality_response(True),
            _triage_response(),
        ])
        await triage_feedback(feedback_id, conn=conn)  # must not raise

    row = _fetch(conn, feedback_id)
    assert row["quality_status"] == "genuine"  # local triage result still saved
    assert row["github_issue_number"] is None
