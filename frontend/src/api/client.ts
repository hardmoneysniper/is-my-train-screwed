export interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
}

export interface ChatResponse {
  reply: string
}

const ANONYMOUS_ID_KEY = 'imts_anonymous_id'

export function getOrCreateAnonymousId(): string {
  try {
    const existing = localStorage.getItem(ANONYMOUS_ID_KEY)
    if (existing) return existing
    const id = crypto.randomUUID()
    localStorage.setItem(ANONYMOUS_ID_KEY, id)
    return id
  } catch {
    // localStorage unavailable (e.g. private browsing, storage disabled) --
    // degrade to a per-session id rather than crashing the send flow.
    // The user simply won't get cross-session monitoring continuity.
    return crypto.randomUUID()
  }
}

export async function sendChatMessage(
  message: string,
  conversationHistory: ChatMessage[]
): Promise<ChatResponse> {
  const response = await fetch('/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      message,
      conversation_history: conversationHistory,
      anonymous_id: getOrCreateAnonymousId(),
    }),
  })
  if (!response.ok) {
    throw new Error(`Chat request failed with status ${response.status}`)
  }
  return response.json()
}
