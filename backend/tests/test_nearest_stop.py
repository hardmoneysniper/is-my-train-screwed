import csv
import pathlib
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
