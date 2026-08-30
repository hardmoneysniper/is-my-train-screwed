import { useEffect, useRef, useState } from 'react'
import type { FormEvent, KeyboardEvent } from 'react'
import { sendChatMessage } from './api/client'
import type { ChatMessage } from './api/client'
import './App.css'

interface DisplayMessage {
  role: 'user' | 'assistant'
  text: string
}

// Matches a whole line that is entirely wrapped in a single pair of
// asterisks, e.g. "*Based on 500 observed patterns in the last 14 days.*"
// -- the citation-footer convention from Task 11. Only ever applied to the
// LAST line of a message (see splitFooter below); an interior line that
// happens to look like this is left as plain text.
const FOOTER_LINE_RE = /^\*(.+)\*$/

// Splits a message body into its main text and an optional trailing
// footer line. Returns `footer: null` unless the message's LAST line
// matches FOOTER_LINE_RE, in which case `body` is everything before it
// (trailing newline trimmed) and `footer` is that line with its wrapping
// asterisks stripped.
function splitFooter(text: string): { body: string; footer: string | null } {
  const lines = text.split('\n')
  const lastLine = lines[lines.length - 1]
  const match = FOOTER_LINE_RE.exec(lastLine)
  if (!match) {
    return { body: text, footer: null }
  }
  return { body: lines.slice(0, -1).join('\n'), footer: match[1] }
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
          {messages.map((m, i) => {
            // splitFooter's citation-footer convention (Task 11) only ever
            // applies to assistant replies -- a user who happens to type a
            // line wrapped in single asterisks must see it rendered back
            // exactly as typed, not split into a footer.
            if (m.role !== 'assistant') {
              return (
                <div key={i} className={`bubble bubble-${m.role}`}>
                  {m.text}
                </div>
              )
            }
            const { body, footer } = splitFooter(m.text)
            return (
              <div key={i} className={`bubble bubble-${m.role}`}>
                {body}
                {footer !== null && (
                  <>
                    {body && <br />}
                    <span className="bubble-footer">{footer}</span>
                  </>
                )}
              </div>
            )
          })}
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
