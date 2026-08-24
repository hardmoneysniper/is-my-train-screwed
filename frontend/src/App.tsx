import { useEffect, useRef, useState } from 'react'
import type { FormEvent, KeyboardEvent } from 'react'
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
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  const started = messages.length > 0

  // Auto-grow the composer to fit its content (up to the CSS max-height
  // cap in App.css, where it switches to an internal scrollbar instead).
  useEffect(() => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${el.scrollHeight}px`
  }, [input])

  async function sendMessage() {
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

  function handleSubmit(e: FormEvent) {
    e.preventDefault()
    void sendMessage()
  }

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    // Enter sends; Shift+Enter inserts a newline. Needed now that this is a
    // <textarea> -- unlike <input>, a textarea's default Enter behavior is
    // to insert a newline, not submit the form, so without this the multi-
    // line composer would have no way to send a message via the keyboard.
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      void sendMessage()
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
        <textarea
          ref={textareaRef}
          className="composer-input"
          rows={1}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
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
