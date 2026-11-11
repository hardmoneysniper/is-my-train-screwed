"""backend/scripts/bus_coverage_checkin_reminder.py

One-time reminder (scheduled via Windows Task Scheduler, see CLAUDE.md's
2026-11-11 section) to come back and ask Claude to re-measure real bus
bucket coverage against the 2026-11-11 baseline
(data/bus_coverage_baseline_2026-11-11.json) -- a real empirical
before/after comparison is far more trustworthy than a single-point
projection (the 2026-10-12 projection for this exact metric was later
confirmed off by about 6x).

This is a pure human reminder, not an automated re-check: nothing in
this repo can re-invoke Claude on its own, and the actual coverage
re-measurement needs a live Claude session to run the comparison script
and interpret it, same as the 2026-11-11 session did. Windows-only
(ctypes.windll), consistent with every other locally-scheduled script in
this project.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.windows_notify import show_message_box  # noqa: E402


def main() -> None:
    show_message_box(
        "Is My Train Screwed? -- Bus Coverage Check-in",
        "It's been ~8 days since the 2026-11-11 bus-coverage baseline.\n\n"
        "Ask Claude to re-measure real bus bucket coverage and compare "
        "against data/bus_coverage_baseline_2026-11-11.json to get a real "
        "(not guessed) days-to-90% estimate.",
    )


if __name__ == "__main__":
    main()
