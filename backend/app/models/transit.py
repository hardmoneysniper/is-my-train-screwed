from pydantic import BaseModel


class WalkStep(BaseModel):
    street_name: str | None = None
    distance_meters: float
    relative_direction: str | None = None
    absolute_direction: str | None = None
    exit: str | None = None
    stay_on: bool = False


class Leg(BaseModel):
    mode: str
    agency: str = "MTA"
    route_short_name: str | None = None
    from_stop_id: str | None = None
    from_stop_name: str
    to_stop_id: str | None = None
    to_stop_name: str
    start_time_ms: int
    end_time_ms: int
    real_time: bool = False
    arrival_delay_seconds: int | None = None
    headsign: str | None = None
    steps: list[WalkStep] = []
    from_lat: float | None = None
    from_lon: float | None = None
    to_lat: float | None = None
    to_lon: float | None = None


class Itinerary(BaseModel):
    duration_seconds: int
    legs: list[Leg]
