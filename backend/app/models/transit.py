from pydantic import BaseModel


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


class Itinerary(BaseModel):
    duration_seconds: int
    legs: list[Leg]
