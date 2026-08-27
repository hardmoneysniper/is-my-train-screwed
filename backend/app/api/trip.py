from fastapi import APIRouter
from pydantic import BaseModel
from app.routing.otp_client import OTPClient
from app.config import settings

router = APIRouter(prefix="/trip", tags=["trip"])


class PlanRequest(BaseModel):
    from_lat: float
    from_lon: float
    to_lat: float
    to_lon: float


@router.post("/plan")
async def plan_trip(req: PlanRequest):
    client = OTPClient(base_url=settings.otp_base_url)
    itineraries = await client.plan_route(req.from_lat, req.from_lon, req.to_lat, req.to_lon)
    return {"itineraries": [it.model_dump() for it in itineraries]}
