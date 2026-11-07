"""backend/scripts/feedback_report.py

Regenerates a human-readable summary of triaged rider feedback
(app/feedback.py + app/agents/feedback_triage_agent.py) from the local
`feedback` table, for the user's 30-day review habit (see
scripts/install_feedback_review_reminder.py, which schedules this script
to run and then open the result). Local-only, gitignored (backend/data/
is gitignored wholesale) -- this is generated output, not source, and
regenerating it is cheap and idempotent (pure read, no DB writes), so
there's no reason to keep stale copies around.

Usage:
    python scripts/feedback_report.py
    python scripts/feedback_report.py --db-path path/to/other.sqlite3
"""
import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.github_issues import issue_url  # noqa: E402
from db import get_connection  # noqa: E402

DEFAULT_DB_PATH = Path(__file__).parent.parent / "data" / "risk.sqlite3"
DEFAULT_OUTPUT_PATH = Path(__file__).parent.parent / "data" / "feedback_report.md"


def _row_line(row) -> str:
    dup_note = f" (likely duplicate of #{row['duplicate_of']})" if row["duplicate_of"] is not None else ""
    if row["github_issue_number"] is not None:
        link = issue_url(row["github_issue_number"])
        issue_note = (
            f" — [issue #{row['github_issue_number']}]({link})" if link else f" — issue #{row['github_issue_number']}"
        )
    else:
        issue_note = ""
    return f"- **#{row['id']}** (trip {row['trip_id']}): {row['summary']}{dup_note}{issue_note}"


def build_report(conn) -> str:
    genuine = conn.execute(
        "SELECT * FROM feedback WHERE quality_status = 'genuine' ORDER BY triaged_at DESC"
    ).fetchall()
    rejected_count = conn.execute(
        "SELECT COUNT(*) AS n FROM feedback WHERE quality_status = 'rejected'"
    ).fetchone()["n"]
    untriaged_count = conn.execute(
        "SELECT COUNT(*) AS n FROM feedback WHERE responded_at IS NOT NULL AND quality_status IS NULL"
    ).fetchone()["n"]
    never_responded_count = conn.execute(
        "SELECT COUNT(*) AS n FROM feedback WHERE responded_at IS NULL"
    ).fetchone()["n"]

    by_category: dict[str, list] = defaultdict(list)
    for row in genuine:
        by_category[row["category"] or "uncategorized"].append(row)

    lines = [
        "# Rider feedback report",
        "",
        f"{len(genuine)} genuine, triaged items. {rejected_count} rejected by the quality "
        f"check (not real feedback). {untriaged_count} captured but not yet triaged "
        f"(triage may still be running, or failed -- see backend logs). "
        f"{never_responded_count} requests sent but the rider never replied.",
        "",
    ]
    for category in sorted(by_category):
        rows = by_category[category]
        lines.append(f"## {category} ({len(rows)})")
        lines.append("")
        lines.extend(_row_line(row) for row in rows)
        lines.append("")
    if not genuine:
        lines.append("Nothing triaged yet.")
    return "\n".join(lines)


def run_report(db_path: Path = DEFAULT_DB_PATH, output_path: Path = DEFAULT_OUTPUT_PATH) -> Path:
    conn = get_connection(str(db_path))
    try:
        report = build_report(conn)
    finally:
        conn.close()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8")
    return output_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--output-path", type=Path, default=DEFAULT_OUTPUT_PATH)
    args = parser.parse_args()
    written_to = run_report(args.db_path, args.output_path)
    print(f"wrote {written_to}")
