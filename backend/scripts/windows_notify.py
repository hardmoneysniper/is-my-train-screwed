"""backend/scripts/windows_notify.py

Tiny shared helper for the blocking Windows message-box reminders this
project schedules via Task Scheduler (scripts/feedback_review_reminder.py,
scripts/bus_coverage_checkin_reminder.py) -- extracted 2026-11-11 after
the same MB_OK/MB_ICONINFORMATION/MessageBoxW call was duplicated
across both. Windows-only (ctypes.windll), consistent with every other
locally-scheduled script in this project.
"""
import ctypes

_MB_OK = 0x0
_MB_ICONINFORMATION = 0x40


def show_message_box(title: str, body: str) -> None:
    ctypes.windll.user32.MessageBoxW(0, body, title, _MB_OK | _MB_ICONINFORMATION)
