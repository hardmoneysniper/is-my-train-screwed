from pathlib import Path
from unittest.mock import patch

from scripts.feedback_review_reminder import main


def test_main_regenerates_the_report_opens_it_and_shows_a_message_box():
    report_path = Path("fake") / "feedback_report.md"
    with (
        patch("scripts.feedback_review_reminder.run_report", return_value=report_path) as mock_run_report,
        patch("scripts.feedback_review_reminder.os.startfile") as mock_startfile,
        patch("scripts.feedback_review_reminder.show_message_box") as mock_show,
    ):
        main()

    mock_run_report.assert_called_once_with()
    mock_startfile.assert_called_once_with(report_path)
    mock_show.assert_called_once()
    message_text = mock_show.call_args.args[1]
    assert str(report_path) in message_text
