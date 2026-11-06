from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

import cost_guard
from app.agents.conversation_agent import ConversationAgent
from app.rate_limit import enforce_chat_rate_limit

router = APIRouter(tags=["chat"])

# Shown instead of a raw 500 if the monthly Anthropic spend cap has been
# hit (cost_guard.CostCapExceeded is deliberately left uncaught all the
# way up to here -- see conversation_agent.py -- so this is the one place
# it should ever be caught, turning "every request 500s with no
# explanation for the rest of the month" into an honest, distinct message
# instead of looking identical to any other failure.
COST_CAP_REPLY = (
    "We've hit this month's usage limit, so I can't chat right now -- "
    "please check back next month, or let the team know if this is urgent."
)


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    message: str
    conversation_history: list[ChatMessage]
    anonymous_id: str  # passed through to respond() for monitored-trip tool dispatch (Task 5);
    # Task 8 additionally uses it inside respond() to claim + prepend pending notifications
    user_location: dict | None = None  # {"lat": float, "lon": float} or None -- walking-nav design doc §2


@router.post("/chat", dependencies=[Depends(enforce_chat_rate_limit)])
async def chat(req: ChatRequest):
    agent = ConversationAgent()
    history = [{"role": m.role, "content": m.content} for m in req.conversation_history]
    try:
        reply = await agent.respond(req.message, history, req.anonymous_id, req.user_location)
    except cost_guard.CostCapExceeded:
        return {"reply": COST_CAP_REPLY}
    return {"reply": reply}
