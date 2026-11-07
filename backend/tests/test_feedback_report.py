from unittest.mock import patch

from scripts.feedback_report import build_report, run_report
from db import get_connection


def _insert(conn, trip_id, quality_status=None, category=None, summary=None,
            duplicate_of=None, responded_at="2026-11-06T00:00:00+00:00", github_issue_number=None):
    conn.execute(
        """
        INSERT INTO feedback (
            trip_id, anonymous_id, requested_at, asked_at, responded_at,
            raw_text, quality_status, category, summary, duplicate_of, triaged_at, github_issue_number
        ) VALUES (?, 'anon-1', '2026-11-06T00:00:00+00:00', '2026-11-06T00:01:00+00:00', ?, 'raw text', ?, ?, ?, ?, ?, ?)
        """,
        (trip_id, responded_at, quality_status, category, summary, duplicate_of,
         "2026-11-06T00:02:00+00:00" if quality_status else None, github_issue_number),
    )
    conn.commit()


def test_report_counts_rejected_and_untriaged_and_unresponded(tmp_path):
    conn = get_connection(str(tmp_path / "report.sqlite3"))
    _insert(conn, 1, quality_status="rejected")
    _insert(conn, 2, quality_status=None)  # captured, triage never ran/finished
    _insert(conn, 3, responded_at=None)  # asked, never answered
    report = build_report(conn)
    conn.close()

    assert "1 rejected" in report
    assert "1 captured but not yet triaged" in report
    assert "1 requests sent but the rider never replied" in report


def test_report_groups_genuine_feedback_by_category(tmp_path):
    conn = get_connection(str(tmp_path / "report.sqlite3"))
    _insert(conn, 1, quality_status="genuine", category="bug", summary="App didn't warn about a delay.")
    _insert(conn, 2, quality_status="genuine", category="praise", summary="Loved the walking directions.")
    report = build_report(conn)
    conn.close()

    assert "## bug (1)" in report
    assert "App didn't warn about a delay." in report
    assert "## praise (1)" in report
    assert "Loved the walking directions." in report


def test_report_notes_a_likely_duplicate(tmp_path):
    conn = get_connection(str(tmp_path / "report.sqlite3"))
    _insert(conn, 1, quality_status="genuine", category="bug", summary="First report.")
    _insert(conn, 2, quality_status="genuine", category="bug", summary="Same thing again.", duplicate_of=1)
    report = build_report(conn)
    conn.close()

    assert "likely duplicate of #1" in report


def test_report_links_a_genuine_items_github_issue(tmp_path):
    conn = get_connection(str(tmp_path / "report.sqlite3"))
    _insert(conn, 1, quality_status="genuine", category="bug", summary="A real bug.", github_issue_number=42)
    with patch(
        "scripts.feedback_report.issue_url",
        return_value="https://github.com/hardmoneysniper/is-my-train-screwed/issues/42",
    ):
        report = build_report(conn)
    conn.close()

    assert "[issue #42](https://github.com/hardmoneysniper/is-my-train-screwed/issues/42)" in report


def test_report_shows_bare_issue_number_when_github_not_configured(tmp_path):
    conn = get_connection(str(tmp_path / "report.sqlite3"))
    _insert(conn, 1, quality_status="genuine", category="bug", summary="A real bug.", github_issue_number=42)
    with patch("scripts.feedback_report.issue_url", return_value=None):
        report = build_report(conn)
    conn.close()

    assert "issue #42" in report
    assert "[issue #42]" not in report  # no markdown link syntax without a real URL


def test_report_omits_issue_note_when_no_issue_was_opened(tmp_path):
    conn = get_connection(str(tmp_path / "report.sqlite3"))
    _insert(conn, 1, quality_status="genuine", category="bug", summary="A real bug.", github_issue_number=None)
    report = build_report(conn)
    conn.close()

    assert "issue #" not in report


def test_report_with_no_feedback_at_all_says_so(tmp_path):
    conn = get_connection(str(tmp_path / "report.sqlite3"))
    report = build_report(conn)
    conn.close()

    assert "Nothing triaged yet." in report


def test_run_report_writes_the_file(tmp_path):
    db_path = tmp_path / "report.sqlite3"
    conn = get_connection(str(db_path))
    _insert(conn, 1, quality_status="genuine", category="bug", summary="A real bug.")
    conn.close()

    output_path = tmp_path / "out" / "feedback_report.md"
    written_to = run_report(db_path, output_path)

    assert written_to == output_path
    assert "A real bug." in output_path.read_text(encoding="utf-8")
