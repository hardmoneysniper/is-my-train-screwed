from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from app.agents.conversation_agent import ConversationAgent

router = APIRouter(tags=["chat"])


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    message: str
    conversation_history: list[ChatMessage]


@router.post("/chat")
async def chat(req: ChatRequest):
    agent = ConversationAgent()
    history = [{"role": m.role, "content": m.content} for m in req.conversation_history]
    reply = await agent.respond(req.message, history)
    return {"reply": reply}
