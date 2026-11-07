import { useEffect, useRef, useState } from 'react'
import type { FormEvent, KeyboardEvent } from 'react'
import { getOrCreateAnonymousId, sendChatMessage } from './api/client'
import type { ChatMessage } from './api/client'
import { subscribeToPush } from './push'
import './App.css'

interface DisplayMessage {
  role: 'user' | 'assistant'
  text: string
  // 'install' marks the one-per-day install-prompt message (see
  // maybeOfferInstall below) so it renders with its own CTA button
  // instead of going through the plain bubble/footer rendering path.
  kind?: 'install'
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

// Chrome/Edge/Android fire this before showing their own native install
// UI; capturing it lets us defer that native prompt to our own button
// instead of it popping up unprompted. Not a standard DOM type yet, so
// this is typed by hand against the one method we actually use.
type BeforeInstallPromptEvent = Event & {
  prompt: () => Promise<void>
  userChoice: Promise<{ outcome: 'accepted' | 'dismissed' }>
}

const INSTALL_PROMPT_DATE_KEY = 'imts_installPromptDate'

// YYYY-MM-DD in the rider's own local time -- a calendar-day cap should
// track the day as the rider experiences it, not UTC.
function todayKey(): string {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

function isStandalone(): boolean {
  return (
    window.matchMedia('(display-mode: standalone)').matches ||
    (window.navigator as unknown as { standalone?: boolean }).standalone === true
  )
}

function isIos(): boolean {
  return /iphone|ipad|ipod/i.test(window.navigator.userAgent)
}

// Android/desktop Chrome (which fire beforeinstallprompt) get a real
// one-click install -- the button below triggers it directly, then
// subscribes to push in the same gesture. iOS Safari has no programmatic
// install API at all, so its copy explains the manual Share-sheet route
// instead of offering a button that couldn't do anything. Both variants
// lead with "not an app" framing (no App Store, no download) -- the
// standard reassurance pattern for PWA install prompts (see Google's PWA
// UX playbook's "no download required" guidance), since "install" reads
// as a bigger ask than this actually is.
const INSTALL_PROMPT_TEXT =
  'Want real-time disruption alerts and instant reroutes? Install this in one click.\n\n' +
  "It's not an app — no App Store, no download. Just a quick, safe, lightweight shortcut to this page on your home screen."
const IOS_INSTALL_PROMPT_TEXT =
  'Want real-time disruption alerts and instant reroutes? Add this to your home screen: tap Share, then "Add to Home Screen."\n\n' +
  "It's not an app — no App Store, no download. Just a quick, safe, lightweight shortcut to this page."

export default function App() {
  const [messages, setMessages] = useState<DisplayMessage[]>([])
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [dotCount, setDotCount] = useState(1)
  const [installEvent, setInstallEvent] = useState<BeforeInstallPromptEvent | null>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  const started = messages.length > 0

  // Android/desktop Chrome offer a native install prompt via this event;
  // iOS Safari never fires it (no programmatic install API there at
  // all), which is why maybeOfferInstall below still offers iOS riders a
  // banner with manual Share-sheet instructions instead of a button.
  useEffect(() => {
    function handler(e: Event) {
      e.preventDefault()
      setInstallEvent(e as BeforeInstallPromptEvent)
    }
    window.addEventListener('beforeinstallprompt', handler)
    return () => window.removeEventListener('beforeinstallprompt', handler)
  }, [])

  // Offers "Add to Home Screen" at most once per calendar day, and only
  // after a reply has actually come back -- never on cold page load, so
  // a first-time visitor sees the app work before being asked to install
  // it. Rendered as a message IN the conversation (not a separate
  // banner) per the user's explicit design: a chat bubble with its own
  // CTA, same visual language as every other assistant message.
  function maybeOfferInstall(hasInstallEvent: boolean) {
    if (isStandalone()) return // already installed, nothing to offer
    if (!hasInstallEvent && !isIos()) return // nothing this browser can act on
    try {
      const today = todayKey()
      if (localStorage.getItem(INSTALL_PROMPT_DATE_KEY) === today) return
      localStorage.setItem(INSTALL_PROMPT_DATE_KEY, today)
    } catch {
      // localStorage unavailable (private mode, etc.) -- fall through and
      // show it anyway rather than silently never offering install.
    }
    setMessages((prev) => [
      ...prev,
      { role: 'assistant', text: isIos() ? IOS_INSTALL_PROMPT_TEXT : INSTALL_PROMPT_TEXT, kind: 'install' },
    ])
  }

  // Android/desktop: triggers the real native install prompt, then
  // subscribes to push in the same user gesture (permission prompts
  // generally need one) -- installing and turning on real-time alerts
  // happen together, matching the single "Install this in one click" CTA.
  // Only subscribes if the rider actually accepted the native prompt --
  // asking for push permission right after they've just DISMISSED one
  // popup would be a second unwanted interruption, not a continuation of
  // the same gesture. installEvent.prompt() itself can also reject (a
  // prompt that's already been resolved/dismissed through another path)
  // -- caught so a stale click can't leave an unhandled rejection.
  async function handleInstallClick() {
    if (!installEvent) return
    try {
      await installEvent.prompt()
      const { outcome } = await installEvent.userChoice
      setInstallEvent(null)
      if (outcome === 'accepted') {
        await subscribeToPush(getOrCreateAnonymousId())
      }
    } catch {
      setInstallEvent(null)
    }
  }

  // Cycles 1 -> 2 -> 3 -> 1... while waiting on a reply, rendered as
  // "." / ".." / "..." below. Fast (300ms/step) since this is standing in
  // for a potentially long wait (e.g. an OTP cold start up to ~40s) and
  // needs to read as "alive," not as a slow pulse.
  useEffect(() => {
    if (!sending) {
      setDotCount(1)
      return
    }
    const interval = setInterval(() => {
      setDotCount((prev) => (prev % 3) + 1)
    }, 300)
    return () => clearInterval(interval)
  }, [sending])

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

    // The install-prompt bubble (kind: 'install') is local UI chrome, not
    // something the assistant actually said -- sending it back as
    // conversation_history would make the LLM think it already said
    // that in a prior turn, for the rest of the session.
    const history: ChatMessage[] = messages
      .filter((m) => m.kind !== 'install')
      .map((m) => ({ role: m.role, content: m.text }))
    setMessages((prev) => [...prev, { role: 'user', text: trimmed }])
    setInput('')
    setSending(true)

    try {
      const { reply } = await sendChatMessage(trimmed, history)
      setMessages((prev) => [...prev, { role: 'assistant', text: reply }])
      maybeOfferInstall(installEvent !== null)
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
            if (m.kind === 'install') {
              return (
                <div key={i} className="bubble bubble-assistant install-bubble">
                  {m.text}
                  {installEvent && (
                    <button type="button" className="install-bubble-action" onClick={() => void handleInstallClick()}>
                      Install
                    </button>
                  )}
                </div>
              )
            }
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
          {sending && (
            <div className="bubble bubble-assistant typing-indicator">
              {'.'.repeat(dotCount)}
            </div>
          )}
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
