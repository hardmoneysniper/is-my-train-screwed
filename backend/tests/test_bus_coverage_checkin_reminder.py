from unittest.mock import patch

from scripts.bus_coverage_checkin_reminder import main


def test_main_shows_a_message_box_naming_the_baseline_file():
    with patch("scripts.bus_coverage_checkin_reminder.show_message_box") as mock_show:
        main()

    mock_show.assert_called_once()
    message_text = mock_show.call_args.args[1]
    assert "bus_coverage_baseline_2026-11-11.json" in message_text
