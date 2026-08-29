from datetime import date
from unittest.mock import MagicMock, patch

import httpx
import pytest

from scripts.download_subwaydata import (
    check_day_available,
    daterange,
    run_backfill,
)


def test_daterange_returns_expected_number_of_days_ending_at_end_date():
    end = date(2026, 8, 28)
    days = daterange(end, 90)
    assert len(days) == 90
    assert days[0] == end
    assert days[-1] == date(2026, 5, 31)


def test_check_day_available_true_on_200():
    with patch("scripts.download_subwaydata.httpx.head") as mock_head:
        mock_head.return_value = MagicMock(status_code=200, headers={})
        assert check_day_available("https://example.com/x.tar.xz") is True


def test_check_day_available_false_on_404():
    with patch("scripts.download_subwaydata.httpx.head") as mock_head:
        mock_head.return_value = MagicMock(status_code=404, headers={})
        assert check_day_available("https://example.com/x.tar.xz") is False


def test_check_day_available_raises_on_500():
    with patch("scripts.download_subwaydata.httpx.head") as mock_head:
        response = httpx.Response(500, request=httpx.Request("HEAD", "https://example.com/x.tar.xz"))
        mock_head.return_value = response
        with pytest.raises(httpx.HTTPStatusError):
            check_day_available("https://example.com/x.tar.xz")


def test_run_backfill_downloads_day_that_head_checks_200(tmp_path):
    end = date(2026, 8, 28)
    with patch("scripts.download_subwaydata.check_day_available", return_value=True) as mock_check, \
         patch("scripts.download_subwaydata.download_subwaydata_day") as mock_download:
        run_backfill(tmp_path, end, days=1)

    mock_check.assert_called_once()
    mock_download.assert_called_once()
    called_url, called_dest = mock_download.call_args.args
    assert called_url == "https://subwaydata.nyc/data/subwaydatanyc_2026-08-28_csv.tar.xz"
    assert called_dest == tmp_path / "2026-08-28.tar.xz"


def test_run_backfill_skips_day_that_head_checks_404_without_raising(tmp_path):
    end = date(2026, 8, 28)
    with patch("scripts.download_subwaydata.check_day_available", return_value=False), \
         patch("scripts.download_subwaydata.download_subwaydata_day") as mock_download:
        run_backfill(tmp_path, end, days=1)

    mock_download.assert_not_called()


def test_run_backfill_skips_download_when_destination_already_exists(tmp_path):
    end = date(2026, 8, 28)
    existing = tmp_path / "2026-08-28.tar.xz"
    existing.write_bytes(b"already here")

    with patch("scripts.download_subwaydata.check_day_available") as mock_check, \
         patch("scripts.download_subwaydata.download_subwaydata_day") as mock_download:
        run_backfill(tmp_path, end, days=1)

    mock_check.assert_not_called()
    mock_download.assert_not_called()


def test_run_backfill_propagates_unexpected_failure_status(tmp_path):
    end = date(2026, 8, 28)
    with patch(
        "scripts.download_subwaydata.check_day_available",
        side_effect=httpx.HTTPStatusError(
            "server error", request=MagicMock(), response=MagicMock(status_code=500)
        ),
    ), patch("scripts.download_subwaydata.download_subwaydata_day") as mock_download:
        with pytest.raises(httpx.HTTPStatusError):
            run_backfill(tmp_path, end, days=1)

    mock_download.assert_not_called()


def test_download_subwaydata_day_streams_to_destination_and_creates_parent_dir(tmp_path):
    dest = tmp_path / "nested" / "2026-08-28.tar.xz"

    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.iter_bytes.return_value = [b"chunk1", b"chunk2"]

    class FakeStreamCtx:
        def __enter__(self):
            return fake_response

        def __exit__(self, *exc):
            return False

    with patch("scripts.download_subwaydata.httpx.stream", return_value=FakeStreamCtx()) as mock_stream:
        from scripts.download_subwaydata import download_subwaydata_day

        result = download_subwaydata_day("https://example.com/x.tar.xz", dest)

    mock_stream.assert_called_once()
    assert result == dest
    assert dest.read_bytes() == b"chunk1chunk2"
