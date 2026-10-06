import httpx
from app.models.transit import Itinerary, Leg, WalkStep

PLAN_QUERY = """
query Plan($fromLat: Float!, $fromLon: Float!, $toLat: Float!, $toLon: Float!) {
  plan(from: {lat: $fromLat, lon: $fromLon}, to: {lat: $toLat, lon: $toLon}) {
    itineraries {
      duration
      legs {
        mode
        route { shortName }
        from { name stop { gtfsId } lat lon }
        to { name stop { gtfsId } lat lon }
        startTime
        endTime
        realTime
        arrivalDelay
        headsign
        steps { streetName distance relativeDirection absoluteDirection exit stayOn }
      }
    }
  }
}
"""


class OTPClient:
    def __init__(self, base_url: str):
        self._graphql_url = f"{base_url}/otp/routers/default/index/graphql"

    async def plan_route(self, from_lat: float, from_lon: float, to_lat: float, to_lon: float) -> list[Itinerary]:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self._graphql_url,
                json={
                    "query": PLAN_QUERY,
                    "variables": {"fromLat": from_lat, "fromLon": from_lon, "toLat": to_lat, "toLon": to_lon},
                },
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()["data"]["plan"]["itineraries"]

        return [
            Itinerary(
                duration_seconds=it["duration"],
                legs=[
                    Leg(
                        mode=leg["mode"],
                        route_short_name=(leg.get("route") or {}).get("shortName"),
                        from_stop_id=(leg["from"].get("stop") or {}).get("gtfsId"),
                        from_stop_name=leg["from"]["name"],
                        to_stop_id=(leg["to"].get("stop") or {}).get("gtfsId"),
                        to_stop_name=leg["to"]["name"],
                        start_time_ms=leg["startTime"],
                        end_time_ms=leg["endTime"],
                        real_time=leg.get("realTime", False),
                        arrival_delay_seconds=leg.get("arrivalDelay"),
                        headsign=leg.get("headsign"),
                        steps=[
                            WalkStep(
                                street_name=step.get("streetName"),
                                distance_meters=step["distance"],
                                relative_direction=step.get("relativeDirection"),
                                absolute_direction=step.get("absoluteDirection"),
                                exit=step.get("exit"),
                                stay_on=step.get("stayOn", False),
                            )
                            for step in (leg.get("steps") or [])
                        ],
                        from_lat=leg["from"].get("lat"),
                        from_lon=leg["from"].get("lon"),
                        to_lat=leg["to"].get("lat"),
                        to_lon=leg["to"].get("lon"),
                    )
                    for leg in it["legs"]
                ],
            )
            for it in data
        ]
