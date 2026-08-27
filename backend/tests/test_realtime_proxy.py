import pathlib
import pytest
from app.realtime_proxy import TripIndex

SUBWAY_ZIP = pathlib.Path(__file__).parent.parent / "data" / "gtfs" / "subway.zip"


@pytest.fixture(scope="module")
def trip_index():
    return TripIndex(SUBWAY_ZIP)


def test_resolves_unambiguous_suffix(trip_index):
    # Use a real trip_id from the actual downloaded static feed rather than
    # a fabricated one -- read one directly to keep this test honest about
    # what real data looks like.
    import csv, io, zipfile
    with zipfile.ZipFile(SUBWAY_ZIP) as z:
        with z.open("trips.txt") as f:
            rows = list(csv.DictReader(io.TextIOWrapper(f, encoding="utf-8")))
    # find a trip_id whose suffix is unique across the whole static feed
    from collections import Counter
    suffixes = Counter(r["trip_id"].split("_", 1)[1] for r in rows if "_" in r["trip_id"])
    unique_row = next(r for r in rows if "_" in r["trip_id"] and suffixes[r["trip_id"].split("_", 1)[1]] == 1)
    rt_style_id = unique_row["trip_id"].split("_", 1)[1]

    resolved = trip_index.resolve(rt_style_id, start_date="20260101")
    assert resolved == unique_row["trip_id"]


def test_returns_none_for_unmatched_suffix(trip_index):
    assert trip_index.resolve("999999_ZZ..X99R", start_date="20260101") is None


def test_returns_none_for_ambiguous_suffix_with_no_active_service(trip_index):
    # A suffix shared by multiple trips, queried on a date where neither
    # candidate's service is active, must not guess.
    import csv, io, zipfile
    from collections import Counter
    with zipfile.ZipFile(SUBWAY_ZIP) as z:
        with z.open("trips.txt") as f:
            rows = list(csv.DictReader(io.TextIOWrapper(f, encoding="utf-8")))
    suffixes = Counter(r["trip_id"].split("_", 1)[1] for r in rows if "_" in r["trip_id"])
    shared = [s for s, count in suffixes.items() if count > 1]
    if not shared:
        pytest.skip("no shared suffixes in this static feed snapshot to test ambiguity with")
    resolved = trip_index.resolve(shared[0], start_date="19000101")  # date far outside any real service window
    assert resolved is None
