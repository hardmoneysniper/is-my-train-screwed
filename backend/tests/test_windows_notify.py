from unittest.mock import MagicMock, patch

from scripts.windows_notify import show_message_box


def test_show_message_box_calls_the_real_win32_api_with_title_and_body():
    with patch("scripts.windows_notify.ctypes.windll", create=True) as mock_windll:
        show_message_box("A Title", "A Body")

    mock_windll.user32.MessageBoxW.assert_called_once()
    args = mock_windll.user32.MessageBoxW.call_args.args
    assert args[1] == "A Body"
    assert args[2] == "A Title"
