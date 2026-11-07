"""backend/scripts/feedback_review_reminder.py

Scheduled via Windows Task Scheduler (task "IsMyTrainScrewed-FeedbackReview",
registered with `schtasks /sc daily /mo 30` -- a DAILY schedule's /mo
modifier means "every N days", so this fires every 30 days, same
mechanism used for the real `/sc daily` tasks this project already
registers elsewhere; see CLAUDE.md's Task Scheduler notes for the F:\
drive launch gotcha this follows) to fire every 30 days: regenerates the
feedback report (scripts/feedback_report.py) then opens it and pops a
blocking message box pointing at it. A silently regenerated file is easy
to never notice -- this makes the 30-day review an actual interruption
clicked through on the machine, not something to stumble on days later.

Windows-only (ctypes.windll, os.startfile) -- consistent with every
other locally-scheduled script in this project, all of which already
assume this machine's OS.
"""
import ctypes
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.feedback_report import run_report  # noqa: E402

MB_OK = 0x0
MB_ICONINFORMATION = 0x40


def main() -> None:
    report_path = run_report()
    os.startfile(report_path)  # opens in whatever app .md is associated with
    ctypes.windll.user32.MessageBoxW(
        0,
        f"Time for your 30-day feedback review.\n\n{report_path} has just been regenerated and opened.",
        "Is My Train Screwed? -- Feedback Review",
        MB_OK | MB_ICONINFORMATION,
    )


if __name__ == "__main__":
    main()
