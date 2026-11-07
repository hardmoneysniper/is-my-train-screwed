from fastapi import APIRouter
from pydantic import BaseModel

from app.config import settings
from app.push import save_subscription

router = APIRouter(tags=["push"])


class PushSubscriptionPayload(BaseModel):
    # The raw PushSubscription.toJSON() shape from the browser. Only
    # `endpoint` is declared -- it's the one field app/push.py actually
    # depends on (save_subscription keys rows by it); everything else
    # (keys, expirationTime) passes through via `extra="allow"` rather
    # than being re-modeled field-for-field. Declaring it at all (instead
    # of accepting a bare `dict`, the original shape here) means a
    # malformed/missing endpoint fails with FastAPI's normal clean 422,
    # not a 500 from save_subscription's own `subscription["endpoint"]`
    # KeyError -- same "never let bad input 500" discipline as
    # app/rate_limit.py's enforce_chat_rate_limit.
    endpoint: str

    model_config = {"extra": "allow"}


class PushSubscribeRequest(BaseModel):
    anonymous_id: str
    subscription: PushSubscriptionPayload


@router.get("/push/public-key")
def public_key():
    """The VAPID public key the frontend passes as `applicationServerKey`
    to `PushManager.subscribe` -- not a secret (it's sent to the push
    service and to every subscriber by design), unlike the private key."""
    return {"public_key": settings.vapid_public_key}


@router.post("/push/subscribe")
def subscribe(req: PushSubscribeRequest):
    save_subscription(req.anonymous_id, req.subscription.model_dump())
    return {"ok": True}
