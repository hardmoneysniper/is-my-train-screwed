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
            "anonymous_id": "11111111-1111-1111-1111-111111111111",
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
            "anonymous_id": "11111111-1111-1111-1111-111111111111",
        })
    assert response.status_code == 200
    mock_respond.assert_awaited_once_with(
        "and then?",
        [
            {"role": "user", "content": "plan a trip"},
            {"role": "assistant", "content": "where to?"},
        ],
        "11111111-1111-1111-1111-111111111111",
    )


def test_chat_missing_message_returns_422():
    response = client.post("/chat", json={
        "conversation_history": [],
        "anonymous_id": "11111111-1111-1111-1111-111111111111",
    })
    assert response.status_code == 422


def test_chat_passes_anonymous_id_through_to_respond():
    with patch("app.api.chat.ConversationAgent.respond", new_callable=AsyncMock) as mock_respond:
        mock_respond.return_value = "sure"
        response = client.post("/chat", json={
            "message": "and then?",
            "conversation_history": [],
            "anonymous_id": "22222222-2222-2222-2222-222222222222",
        })
    assert response.status_code == 200
    mock_respond.assert_awaited_once_with(
        "and then?", [], "22222222-2222-2222-2222-222222222222"
    )


def test_chat_missing_anonymous_id_returns_422():
    response = client.post("/chat", json={
        "message": "hi",
        "conversation_history": [],
    })
    assert response.status_code == 422
