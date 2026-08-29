from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_chat_returns_reply_with_empty_history():
    with patch("app.api.chat.ConversationAgent.respond", new_callable=AsyncMock) as mock_respond:
        mock_respond.return_value = "I can help you plan a trip."
        response = client.post("/chat", json={
            "message": "hi, what can you help me with?",
            "conversation_history": [],
        })
    assert response.status_code == 200
    assert response.json() == {"reply": "I can help you plan a trip."}


def test_chat_converts_conversation_history_to_dicts():
    with patch("app.api.chat.ConversationAgent.respond", new_callable=AsyncMock) as mock_respond:
        mock_respond.return_value = "sure"
        response = client.post("/chat", json={
            "message": "and then?",
            "conversation_history": [
                {"role": "user", "content": "plan a trip"},
                {"role": "assistant", "content": "where to?"},
            ],
        })
    assert response.status_code == 200
    mock_respond.assert_awaited_once_with(
        "and then?",
        [
            {"role": "user", "content": "plan a trip"},
            {"role": "assistant", "content": "where to?"},
        ],
    )


def test_chat_missing_message_returns_422():
    response = client.post("/chat", json={"conversation_history": []})
    assert response.status_code == 422
