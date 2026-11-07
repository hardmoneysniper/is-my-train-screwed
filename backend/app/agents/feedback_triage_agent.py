"""backend/app/agents/feedback_triage_agent.py

Phase 4's Feedback Triage Agent (CLAUDE.md), built 2026-11-06, GitHub
issue integration added 2026-11-07. Two small LLM calls (default Haiku,
per the model-choice rule -- classifying/summarizing text is not
"computing a number, route, or probability", so this doesn't violate the
hard rule against LLM-guessed numbers), run once per captured feedback
response (triggered fire-and-forget right after
`app.feedback.claim_awaiting_feedback_response` captures the raw text --
see `app/agents/conversation_agent.py`):

1. Quality check: is this actually trip feedback, or did the rider just
   ignore the prompt and type something unrelated (a new trip question,
   a greeting, spam)? `app/feedback.py`'s module docstring explains why
   the server-side capture itself is unconditional; this step is the
   safety net that catches it when that guess was wrong. Rejected
   feedback is marked and excluded from triage -- never silently
   discarded (the raw text and the rejection both stay in the row).
2. Triage (genuine feedback only): categorize, write a one-sentence
   summary, and check it against a recent window of other genuine
   summaries for a likely duplicate. One call does all three -- cheaper
   and simpler than three separate requests for what's fundamentally one
   small judgment call. The model's own `duplicate_of` answer is
   validated against the real candidate ids it was shown (see
   `_validate_duplicate_of`) before ever being trusted -- a model that
   names an id it was never offered, or isn't even an integer, is
   treated the same as "no duplicate," never stored or acted on blind.

3. GitHub sync (app/github_issues.py): a genuine, non-duplicate item
   opens a real GitHub issue (category as its label) and records the
   issue number. A genuine duplicate instead comments on the ORIGINAL's
   issue -- unless that original has no recorded issue number (GitHub
   wasn't configured, or that earlier call failed) OR commenting on it
   fails right now, in which case this item opens its own issue instead,
   so a GitHub outage on one item never silently loses coverage for
   every later "duplicate" of it.
   Best-effort throughout (see github_issues.py's own docstring) -- a
   GitHub failure here never prevents the local triage result (quality_
   status/category/summary/duplicate_of) from being saved; it only means
   github_issue_number stays NULL.
"""
import json
import sqlite3
from datetime import datetime, timezone

from anthropic import AsyncAnthropic

import cost_guard
from app.config import settings
from app.github_issues import comment_on_issue, create_issue
from db import get_connection

QUALITY_CHECK_PROMPT = (
    "A rider of a NYC transit trip-advisor app was asked for feedback on a "
    "trip that just ended. Decide whether their reply below is genuine "
    "feedback about the app or the trip (a complaint, praise, bug report, "
    "or suggestion) versus something unrelated (e.g. a new trip question, "
    "a greeting, spam, or an empty/non-answer).\n\n"
    "Respond with ONLY a JSON object: {{\"is_feedback\": true or false}}.\n\n"
    "Rider's reply: {text}"
)

TRIAGE_PROMPT = (
    "Triage this genuine piece of rider feedback for a NYC transit trip-"
    "advisor app.\n\n"
    "Feedback: {text}\n\n"
    "Recent other feedback summaries, for duplicate-detection only "
    "(id: summary):\n{recent}\n\n"
    "Respond with ONLY a JSON object with these keys:\n"
    "- category: one of \"bug\", \"feature_request\", \"data_quality\", "
    "\"ux\", \"praise\", \"other\"\n"
    "- summary: a single plain sentence summarizing the feedback\n"
    "- duplicate_of: the id (integer) of a recent summary above that says "
    "essentially the same thing, or null if none do"
)

_RECENT_SUMMARIES_LIMIT = 50
VALID_CATEGORIES = frozenset({"bug", "feature_request", "data_quality", "ux", "praise", "other"})
DEFAULT_CATEGORY = "other"


def _parse_json_object(text: str) -> dict:
    """Both prompts ask for JSON-only output, but a model occasionally
    wraps it in prose or a markdown fence anyway -- extract the first
    {...} block rather than assuming the response text is bare JSON."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object found in model output: {text!r}")
    return json.loads(text[start:end + 1])


def _validate_category(raw_value) -> str:
    """Never trusts the model's category string blind -- an unrecognized
    value (a typo, a category it invented, None) would otherwise both
    fragment scripts/feedback_report.py's grouping with one-off labels
    AND create an arbitrary new GitHub label per occurrence. Falls back
    to DEFAULT_CATEGORY rather than storing it as-is. The isinstance
    check must run (and short-circuit) before the `in` test -- `x in
    frozenset(...)` requires `x` to be hashable, and a model returning a
    list or dict for this field (plausible JSON-shaped noise) would
    otherwise raise TypeError instead of just failing validation."""
    return raw_value if isinstance(raw_value, str) and raw_value in VALID_CATEGORIES else DEFAULT_CATEGORY


def _validate_duplicate_of(raw_value, candidate_ids: set[int]) -> int | None:
    """Never trusts the model's duplicate_of answer blind -- it must be
    an id that was actually offered to it in this call's `recent` list.
    A hallucinated id, an id of a row that isn't `recent_rows` eligible
    (wrong quality_status, no summary yet), or a non-integer value (the
    model returning a string, a float, or omitting the key) is all
    treated identically to "no duplicate" rather than stored/acted on."""
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return None
    return value if value in candidate_ids else None


async def _call(client: AsyncAnthropic, model: str, prompt: str) -> dict:
    response = await client.messages.create(
        model=model, max_tokens=256, messages=[{"role": "user", "content": prompt}],
    )
    cost_guard.log_call(response.usage.model_dump(), agent="feedback_triage_agent", model=model)
    text_block = next((b for b in response.content if b.type == "text"), None)
    return _parse_json_object(text_block.text if text_block is not None else "")


def _issue_body(raw_text: str, feedback_id: int) -> str:
    return (
        f"{raw_text}\n\n---\n*Automatically triaged by the Feedback Triage Agent "
        f"from an anonymous rider's post-trip feedback (feedback id {feedback_id}).*"
    )


async def _sync_to_github(conn: sqlite3.Connection, feedback_id: int, raw_text: str, category: str,
                            summary: str, duplicate_of: int | None) -> None:
    """Opens a new issue, or comments on the original's issue for a
    duplicate -- see module docstring. Writes github_issue_number back
    for THIS feedback row only when it ends up owning a new issue (never
    for a duplicate that successfully comments on another issue instead)."""
    if duplicate_of is not None:
        original = conn.execute(
            "SELECT github_issue_number FROM feedback WHERE id = ?", (duplicate_of,)
        ).fetchone()
        original_issue_number = original["github_issue_number"] if original is not None else None
        if original_issue_number is not None:
            commented = await comment_on_issue(
                original_issue_number,
                f"Another rider reported something similar:\n\n> {raw_text}\n\n*(feedback id {feedback_id})*",
            )
            if commented:
                return  # commented on the existing issue -- this row doesn't own its own issue
            # Comment failed (GitHub down, rate-limited, etc.) -- fall
            # through to opening a new issue rather than silently losing
            # GitHub coverage for this item entirely.

    issue_number = await create_issue(summary, _issue_body(raw_text, feedback_id), labels=[category])
    if issue_number is not None:
        conn.execute("UPDATE feedback SET github_issue_number = ? WHERE id = ?", (issue_number, feedback_id))
        conn.commit()


async def triage_feedback(feedback_id: int, conn: sqlite3.Connection | None = None) -> None:
    """Runs the quality-check + triage (+ GitHub sync) pipeline for one
    captured feedback row, writing its result back in place. Raises on
    failure (a malformed model response, a network error, a cost-cap
    breach) rather than swallowing it -- this is always invoked as a
    fire-and-forget background task whose caller already wraps it in its
    own try/except (see conversation_agent.py), so logging the failure
    there is the one place it should happen, not duplicated here."""
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM feedback WHERE id = ?", (feedback_id,)).fetchone()
        if row is None or not row["raw_text"]:
            return

        client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        now_iso = datetime.now(timezone.utc).isoformat()

        quality = await _call(
            client, settings.feedback_quality_model,
            QUALITY_CHECK_PROMPT.format(text=row["raw_text"]),
        )
        if not quality.get("is_feedback"):
            conn.execute(
                "UPDATE feedback SET quality_status = 'rejected', triaged_at = ? WHERE id = ?",
                (now_iso, feedback_id),
            )
            conn.commit()
            return

        recent_rows = conn.execute(
            """
            SELECT id, summary FROM feedback
            WHERE quality_status = 'genuine' AND summary IS NOT NULL AND id != ?
            ORDER BY triaged_at DESC LIMIT ?
            """,
            (feedback_id, _RECENT_SUMMARIES_LIMIT),
        ).fetchall()
        recent_text = "\n".join(f"{r['id']}: {r['summary']}" for r in recent_rows) or "(none yet)"
        candidate_ids = {r["id"] for r in recent_rows}

        triage = await _call(
            client, settings.feedback_triage_model,
            TRIAGE_PROMPT.format(text=row["raw_text"], recent=recent_text),
        )
        category = _validate_category(triage.get("category"))
        summary = triage.get("summary") or row["raw_text"]
        duplicate_of = _validate_duplicate_of(triage.get("duplicate_of"), candidate_ids)

        conn.execute(
            """
            UPDATE feedback SET
                quality_status = 'genuine', category = ?, summary = ?,
                duplicate_of = ?, triaged_at = ?
            WHERE id = ?
            """,
            (category, summary, duplicate_of, now_iso, feedback_id),
        )
        conn.commit()

        await _sync_to_github(conn, feedback_id, row["raw_text"], category, summary, duplicate_of)
    finally:
        if owns_conn:
            conn.close()
