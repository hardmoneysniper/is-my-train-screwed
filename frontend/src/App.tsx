import { useState } from 'react'
import type { FormEvent } from 'react'
import { sendChatMessage } from './api/client'
import type { ChatMessage } from './api/client'
import './App.css'

interface DisplayMessage {
  role: 'user' | 'assistant'
  text: string
}

export default function App() {
  const [messages, setMessages] = useState<DisplayMessage[]>([])
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)

  const started = messages.length > 0

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    const trimmed = input.trim()
    if (!trimmed || sending) return

    const history: ChatMessage[] = messages.map((m) => ({ role: m.role, content: m.text }))
    setMessages((prev) => [...prev, { role: 'user', text: trimmed }])
    setInput('')
    setSending(true)

    try {
      const { reply } = await sendChatMessage(trimmed, history)
      setMessages((prev) => [...prev, { role: 'assistant', text: reply }])
    } catch {
      setMessages((prev) => [
        ...prev,
        { role: 'assistant', text: 'Something went wrong — try again.' },
      ])
    } finally {
      setSending(false)
    }
  }

  return (
    <div className={`app ${started ? 'started' : 'hero'}`}>
      {!started && <h1 className="hero-title">Is my train screwed?</h1>}

      {started && (
        <div className="conversation">
          {messages.map((m, i) => (
            <div key={i} className={`bubble bubble-${m.role}`}>
              {m.text}
            </div>
          ))}
        </div>
      )}

      <form className="composer" onSubmit={handleSubmit}>
        <input
          className="composer-input"
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder={started ? 'Ask any follow up questions' : 'Ask about a trip'}
          disabled={sending}
        />
        <button type="submit" className="composer-send" disabled={sending}>
          Send
        </button>
      </form>
    </div>
  )
}
