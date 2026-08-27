from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient
from app.main import app
from app.models.transit import Itinerary, Leg

client = TestClient(app)

def test_plan_trip_returns_itineraries():
    fake_itinerary = Itinerary(
        duration_seconds=1800,
        legs=[Leg(mode="SUBWAY", route_short_name="F", from_stop_id="R01",
                  from_stop_name="Roosevelt Island", to_stop_id="R11",
                  to_stop_name="Lexington Av/63 St", start_time_ms=0, end_time_ms=1800000)],
    )
    with patch("app.api.trip.OTPClient.plan_route", new_callable=AsyncMock) as mock_plan:
        mock_plan.return_value = [fake_itinerary]
        response = client.post("/trip/plan", json={
            "from_lat": 40.7597, "from_lon": -73.9532,
            "to_lat": 40.7644, "to_lon": -73.9656,
        })
    assert response.status_code == 200
    body = response.json()
    assert body["itineraries"][0]["duration_seconds"] == 1800
