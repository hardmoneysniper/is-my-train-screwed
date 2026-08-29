import csv
import pathlib
import zipfile
import pytest
from app.routing.nearest_stop import StopIndex

@pytest.fixture
def sample_stops_txt(tmp_path):
    path = tmp_path / "stops.txt"
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["stop_id", "stop_name", "stop_lat", "stop_lon"])
        # Roosevelt Island station, actual coords
        writer.writerow(["R01", "Roosevelt Island", "40.7597", "-73.9532"])
        # Grand Central, actual coords — far from Roosevelt Island
        writer.writerow(["631", "Grand Central-42 St", "40.7527", "-73.9772"])
    return path

def test_nearest_returns_closest_stop_first(sample_stops_txt):
    index = StopIndex.from_gtfs(sample_stops_txt)
    result = index.nearest(lat=40.7599, lon=-73.9530, k=1)
    assert result[0]["stop_id"] == "R01"

def test_nearest_k_returns_requested_count(sample_stops_txt):
    index = StopIndex.from_gtfs(sample_stops_txt)
    result = index.nearest(lat=40.7599, lon=-73.9530, k=2)
    assert len(result) == 2
    assert result[0]["distance_m"] < result[1]["distance_m"]


def _make_index(*names):
    return StopIndex([
        {"stop_id": f"S{i}", "stop_name": name, "lat": 40.75, "lon": -73.95}
        for i, name in enumerate(names)
    ])


def test_find_by_name_case_insensitive():
    index = _make_index("Roosevelt Island", "Grand Central-42 St")
    result = index.find_by_name("roosevelt island")
    assert [s["stop_name"] for s in result] == ["Roosevelt Island"]


def test_find_by_name_substring_not_just_prefix():
    index = _make_index("Grand Central-42 St", "86 St", "125 St")
    result = index.find_by_name("St")
    assert {s["stop_name"] for s in result} == {"Grand Central-42 St", "86 St", "125 St"}


def test_find_by_name_sorted_alphabetically():
    index = _make_index("Roosevelt Island", "86 St", "Grand Central-42 St")
    result = index.find_by_name("t")
    names = [s["stop_name"] for s in result]
    assert names == sorted(names)


def test_find_by_name_respects_limit():
    index = _make_index("1 St", "2 St", "3 St", "4 St")
    result = index.find_by_name("St", limit=2)
    assert len(result) == 2


def test_find_by_name_zero_matches_returns_empty_list():
    index = _make_index("Roosevelt Island", "Grand Central-42 St")
    result = index.find_by_name("Nonexistent Place")
    assert result == []


def _write_stops_zip(zip_path, rows):
    csv_lines = ["stop_id,stop_name,stop_lat,stop_lon"]
    for stop_id, stop_name, lat, lon in rows:
        csv_lines.append(f"{stop_id},{stop_name},{lat},{lon}")
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr("stops.txt", "\n".join(csv_lines))


def test_from_gtfs_zips_merges_stops_from_all_zips(tmp_path):
    zip1 = tmp_path / "subway.zip"
    zip2 = tmp_path / "bus_queens.zip"
    zip3 = tmp_path / "bus_bronx.zip"
    _write_stops_zip(zip1, [("R01", "Roosevelt Island", "40.7597", "-73.9532")])
    _write_stops_zip(zip2, [("Q001", "Main St / Kissena Blvd", "40.7590", "-73.8300")])
    _write_stops_zip(zip3, [("BX001", "Fordham Rd", "40.8610", "-73.8990")])

    index = StopIndex.from_gtfs_zips([zip1, zip2, zip3])

    names = {s["stop_name"] for s in index.find_by_name("")}
    assert names == {"Roosevelt Island", "Main St / Kissena Blvd", "Fordham Rd"}
