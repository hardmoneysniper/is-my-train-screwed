from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.feedback_review_reminder import main


def test_main_regenerates_the_report_opens_it_and_shows_a_message_box():
    report_path = Path("fake") / "feedback_report.md"
    with (
        patch("scripts.feedback_review_reminder.run_report", return_value=report_path) as mock_run_report,
        patch("scripts.feedback_review_reminder.os.startfile") as mock_startfile,
        patch("scripts.feedback_review_reminder.ctypes.windll", create=True) as mock_windll,
    ):
        main()

    mock_run_report.assert_called_once_with()
    mock_startfile.assert_called_once_with(report_path)
    mock_windll.user32.MessageBoxW.assert_called_once()
    message_text = mock_windll.user32.MessageBoxW.call_args.args[1]
    assert str(report_path) in message_text
