import pytest
from unittest.mock import patch, MagicMock
from scripts.load_static_gtfs import verify_gtfs_url

def test_verify_gtfs_url_true_on_200_zip():
    with patch("scripts.load_static_gtfs.httpx.head") as mock_head:
        mock_head.return_value = MagicMock(status_code=200, headers={"content-type": "application/zip"})
        assert verify_gtfs_url("https://example.com/google_transit.zip") is True

def test_verify_gtfs_url_false_on_404():
    with patch("scripts.load_static_gtfs.httpx.head") as mock_head:
        mock_head.return_value = MagicMock(status_code=404, headers={})
        assert verify_gtfs_url("https://example.com/missing.zip") is False
