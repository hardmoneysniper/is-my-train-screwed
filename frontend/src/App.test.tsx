import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import App from './App'
import { subscribeToPush } from './push'

vi.mock('./push', () => ({ subscribeToPush: vi.fn() }))

function mockChatReply(reply: string) {
  return vi.spyOn(global, 'fetch').mockResolvedValue({ ok: true, json: async () => ({ reply }) } as Response)
}

function dispatchBeforeInstallPrompt(outcome: 'accepted' | 'dismissed' = 'accepted') {
  const event = new Event('beforeinstallprompt', { cancelable: true }) as Event & {
    prompt: () => Promise<void>
    userChoice: Promise<{ outcome: 'accepted' | 'dismissed' }>
  }
  event.prompt = vi.fn().mockResolvedValue(undefined)
  event.userChoice = Promise.resolve({ outcome })
  window.dispatchEvent(event)
  return event
}

async function sendOneMessage(text = 'How do I get to Lex/63?') {
  fireEvent.change(screen.getByPlaceholderText(/ask (about a trip|any follow up questions)/i), {
    target: { value: text },
  })
  fireEvent.click(screen.getByText(/send/i))
  await waitFor(() => {
    expect(screen.getByText(/take the f train/i)).toBeInTheDocument()
  })
}

describe('App', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    // restoreAllMocks only resets vi.spyOn spies -- it does NOT clear
    // call history on the module-level vi.fn() from the ./push mock
    // factory above, so without this a later test could see a call
    // count left over from an earlier test (caught live: the dismissed-
    // outcome test below was failing because an EARLIER test's
    // subscribeToPush call was still "remembered").
    vi.clearAllMocks()
    localStorage.clear()
    Object.defineProperty(window.navigator, 'userAgent', {
      value: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0',
      configurable: true,
    })
  })

  it('shows the centered hero title and an input before any message is sent', () => {
    render(<App />)
    expect(screen.getByText(/is my train screwed\?/i)).toBeInTheDocument()
    expect(screen.getByPlaceholderText(/ask about a trip/i)).toBeInTheDocument()
  })

  it('sends a chat message, renders both bubbles, and docks the input with the follow-up placeholder', async () => {
    vi.spyOn(global, 'fetch').mockResolvedValue({
      ok: true,
      json: async () => ({ reply: 'Take the F train — about 30 minutes.' }),
    } as Response)

    render(<App />)
    fireEvent.change(screen.getByPlaceholderText(/ask about a trip/i), {
      target: { value: 'How do I get to Lex/63?' },
    })
    fireEvent.click(screen.getByText(/send/i))

    // user bubble renders immediately, before the fetch resolves
    expect(screen.getByText('How do I get to Lex/63?')).toBeInTheDocument()

    await waitFor(() => {
      expect(screen.getByText(/take the f train/i)).toBeInTheDocument()
    })

    // input has dropped to the bottom state with the follow-up placeholder
    expect(screen.getByPlaceholderText(/ask any follow up questions/i)).toBeInTheDocument()
    // hero title is gone once the conversation has started
    expect(screen.queryByText(/is my train screwed\?/i)).not.toBeInTheDocument()
  })

  it('shows a fallback message if the request fails', async () => {
    vi.spyOn(global, 'fetch').mockResolvedValue({ ok: false } as Response)

    render(<App />)
    fireEvent.change(screen.getByPlaceholderText(/ask about a trip/i), {
      target: { value: 'How do I get to Lex/63?' },
    })
    fireEvent.click(screen.getByText(/send/i))

    await waitFor(() => {
      expect(screen.getByText(/something went wrong/i)).toBeInTheDocument()
    })
  })

  it('sends on Enter but inserts a newline on Shift+Enter', async () => {
    const fetchMock = vi.spyOn(global, 'fetch').mockResolvedValue({
      ok: true,
      json: async () => ({ reply: 'Take the F train — about 30 minutes.' }),
    } as Response)

    render(<App />)
    const composer = screen.getByPlaceholderText(/ask about a trip/i)

    fireEvent.change(composer, { target: { value: 'line one' } })
    fireEvent.keyDown(composer, { key: 'Enter', shiftKey: true })
    // Shift+Enter must not submit.
    expect(fetchMock).not.toHaveBeenCalled()
    expect(screen.queryByText(/is my train screwed\?/i)).toBeInTheDocument() // still on hero, nothing sent

    fireEvent.change(composer, { target: { value: 'line one\nline two' } })
    fireEvent.keyDown(composer, { key: 'Enter' })

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(1)
    })
    // multi-line content is preserved in the sent bubble
    expect(document.querySelector('.bubble-user')?.textContent).toBe('line one\nline two')
  })

  it('renders a trailing "*...*" line as a distinct footer element, asterisks stripped', async () => {
    vi.spyOn(global, 'fetch').mockResolvedValue({
      ok: true,
      json: async () => ({
        reply: 'There is about a 12%* chance of missing that transfer.\n' +
          '*Based on 500 observed patterns in the last 14 days.*',
      }),
    } as Response)

    render(<App />)
    fireEvent.change(screen.getByPlaceholderText(/ask about a trip/i), {
      target: { value: 'What is the risk?' },
    })
    fireEvent.click(screen.getByText(/send/i))

    await waitFor(() => {
      expect(screen.getByText('Based on 500 observed patterns in the last 14 days.')).toBeInTheDocument()
    })

    const footerEl = screen.getByText('Based on 500 observed patterns in the last 14 days.')
    expect(footerEl).toHaveClass('bubble-footer')
    // Asterisks are stripped from the rendered footer text.
    expect(footerEl.textContent).not.toContain('*')
    // The body (with its inline %* citation) still renders as plain text.
    expect(screen.getByText(/12%\*/)).toBeInTheDocument()
  })

  it('renders a message without a trailing "*...*" line identically to plain text (no regression)', async () => {
    vi.spyOn(global, 'fetch').mockResolvedValue({
      ok: true,
      json: async () => ({ reply: "Hi! I'm your NYC transit trip advisor." }),
    } as Response)

    render(<App />)
    fireEvent.change(screen.getByPlaceholderText(/ask about a trip/i), {
      target: { value: 'hello' },
    })
    fireEvent.click(screen.getByText(/send/i))

    await waitFor(() => {
      expect(screen.getByText("Hi! I'm your NYC transit trip advisor.")).toBeInTheDocument()
    })

    // No footer element appears anywhere, and the assistant bubble's full
    // text content is exactly the plain reply -- no stray <br> or wrapper.
    expect(document.querySelector('.bubble-footer')).not.toBeInTheDocument()
    const assistantBubble = document.querySelector('.bubble-assistant')
    expect(assistantBubble?.textContent).toBe("Hi! I'm your NYC transit trip advisor.")
  })

  it('does not treat an interior line that looks like "*something*" as the footer', async () => {
    vi.spyOn(global, 'fetch').mockResolvedValue({
      ok: true,
      json: async () => ({
        reply: '*not a footer*\nThis is the real last line, plain text.',
      }),
    } as Response)

    render(<App />)
    fireEvent.change(screen.getByPlaceholderText(/ask about a trip/i), {
      target: { value: 'test interior line' },
    })
    fireEvent.click(screen.getByText(/send/i))

    await waitFor(() => {
      expect(screen.getByText(/this is the real last line/i)).toBeInTheDocument()
    })

    // The interior "*not a footer*" line is not pulled out into a distinct
    // footer element -- only the LAST line is ever eligible.
    expect(document.querySelector('.bubble-footer')).not.toBeInTheDocument()
    const assistantBubble = document.querySelector('.bubble-assistant')
    expect(assistantBubble?.textContent).toBe(
      '*not a footer*\nThis is the real last line, plain text.'
    )
  })

  it('does not split a footer out of a user message, even if it looks like one', async () => {
    vi.spyOn(global, 'fetch').mockResolvedValue({
      ok: true,
      json: async () => ({ reply: 'Sure, tell me more.' }),
    } as Response)

    render(<App />)
    fireEvent.change(screen.getByPlaceholderText(/ask about a trip/i), {
      target: { value: 'Line one\n*this looks like a footer*' },
    })
    fireEvent.click(screen.getByText(/send/i))

    // The user bubble renders the raw text untouched -- no footer split,
    // no <br> inserted, asterisks preserved.
    const userBubble = document.querySelector('.bubble-user')
    expect(userBubble?.textContent).toBe('Line one\n*this looks like a footer*')
    expect(document.querySelector('.bubble-footer')).not.toBeInTheDocument()

    await waitFor(() => {
      expect(screen.getByText(/sure, tell me more/i)).toBeInTheDocument()
    })
  })

  it('renders multiple inline %*-suffixed percentages in the body as plain text', async () => {
    vi.spyOn(global, 'fetch').mockResolvedValue({
      ok: true,
      json: async () => ({
        reply: 'Option A is 30%* risky, option B is 12%* risky.\n' +
          '*Based on 500 observed patterns in the last 14 days.*',
      }),
    } as Response)

    render(<App />)
    fireEvent.change(screen.getByPlaceholderText(/ask about a trip/i), {
      target: { value: 'compare options' },
    })
    fireEvent.click(screen.getByText(/send/i))

    await waitFor(() => {
      expect(screen.getByText(/option a is 30%\*/i)).toBeInTheDocument()
    })

    // Both inline %* citations render untouched as part of the plain body.
    expect(screen.getByText(/option a is 30%\* risky, option b is 12%\* risky\./i)).toBeInTheDocument()
    // Exactly one footer element for the whole message.
    expect(document.querySelectorAll('.bubble-footer')).toHaveLength(1)
  })

  describe('install prompt', () => {
    it('shows an install bubble with a working Install button after the first reply, on a browser with a native prompt', async () => {
      mockChatReply('Take the F train — about 30 minutes.')

      render(<App />)
      const installEvent = dispatchBeforeInstallPrompt() // after render -- App's useEffect must be mounted to catch it
      await sendOneMessage()

      expect(screen.getByText(/want real-time disruption alerts/i)).toBeInTheDocument()
      expect(screen.getByText(/not an app/i)).toBeInTheDocument()
      const button = screen.getByRole('button', { name: /install/i })

      fireEvent.click(button)

      expect(installEvent.prompt).toHaveBeenCalledTimes(1)
      // subscribeToPush only fires after installEvent.userChoice resolves
      // ('accepted' by default from dispatchBeforeInstallPrompt), which
      // happens on a later microtask than the click itself.
      await waitFor(() => {
        expect(subscribeToPush).toHaveBeenCalledTimes(1)
      })
      expect(subscribeToPush).toHaveBeenCalledWith(expect.any(String))
    })

    it('does not send the install-prompt bubble text back as conversation_history on the next message', async () => {
      // Regression test, caught on review: the install bubble is local
      // UI chrome (kind: 'install'), not something the assistant said --
      // it must never be sent back to the backend as if it were a prior
      // turn, or the LLM would think it already said that.
      const fetchMock = mockChatReply('Take the F train — about 30 minutes.')

      render(<App />)
      dispatchBeforeInstallPrompt()
      await sendOneMessage('first message')
      expect(screen.getByText(/want real-time disruption alerts/i)).toBeInTheDocument()

      fetchMock.mockResolvedValue({ ok: true, json: async () => ({ reply: 'second reply' }) } as Response)
      fireEvent.change(screen.getByPlaceholderText(/ask any follow up questions/i), {
        target: { value: 'second message' },
      })
      fireEvent.click(screen.getByText(/send/i))
      await waitFor(() => {
        expect(screen.getByText('second reply')).toBeInTheDocument()
      })

      const secondCallBody = JSON.parse(fetchMock.mock.calls[1]![1]!.body as string)
      const historyTexts: string[] = secondCallBody.conversation_history.map((m: { content: string }) => m.content)
      expect(historyTexts.some((t) => /want real-time disruption alerts/i.test(t))).toBe(false)
      // The real reply and the user's own message are still there --
      // this isn't a case of the filter dropping everything.
      expect(historyTexts).toContain('Take the F train — about 30 minutes.')
      expect(historyTexts).toContain('first message')
    })

    it('does not subscribe to push if the rider dismisses the native install prompt', async () => {
      mockChatReply('Take the F train — about 30 minutes.')

      render(<App />)
      dispatchBeforeInstallPrompt('dismissed')
      await sendOneMessage()
      const button = screen.getByRole('button', { name: /install/i })

      fireEvent.click(button)

      // Give the dismissed-outcome branch a chance to run, then confirm
      // it deliberately did nothing further -- asking for push
      // permission right after the rider just said no to one popup
      // would be a second unwanted interruption.
      await waitFor(() => {
        expect(screen.queryByRole('button', { name: /install/i })).not.toBeInTheDocument()
      })
      expect(subscribeToPush).not.toHaveBeenCalled()
    })

    it('does not show the install bubble again on a second message the same day', async () => {
      mockChatReply('Take the F train — about 30 minutes.')

      render(<App />)
      dispatchBeforeInstallPrompt()
      await sendOneMessage('first message')
      expect(screen.getAllByText(/want real-time disruption alerts/i)).toHaveLength(1)

      await sendOneMessage('second message')
      expect(screen.getAllByText(/want real-time disruption alerts/i)).toHaveLength(1)
    })

    it('shows Share-sheet instructions with no button on iOS (no native install prompt exists there)', async () => {
      Object.defineProperty(window.navigator, 'userAgent', {
        value: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15',
        configurable: true,
      })
      mockChatReply('Take the F train — about 30 minutes.')

      render(<App />)
      await sendOneMessage()

      expect(screen.getByText(/add this to your home screen/i)).toBeInTheDocument()
      expect(screen.getByText(/not an app/i)).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: /install/i })).not.toBeInTheDocument()
    })

    it('shows no install bubble at all when the app is already running standalone', async () => {
      vi.spyOn(window, 'matchMedia').mockReturnValue({ matches: true } as MediaQueryList)
      mockChatReply('Take the F train — about 30 minutes.')

      render(<App />)
      dispatchBeforeInstallPrompt()
      await sendOneMessage()

      expect(screen.queryByText(/want real-time disruption alerts/i)).not.toBeInTheDocument()
    })

    it('shows no install bubble on a browser with neither a native prompt nor iOS', async () => {
      mockChatReply('Take the F train — about 30 minutes.')
      // No beforeinstallprompt dispatched, and the default userAgent (set
      // in beforeEach) is a plain desktop Chrome string, not iOS -- this
      // is the "nothing this browser can act on" branch.

      render(<App />)
      await sendOneMessage()

      expect(screen.queryByText(/want real-time disruption alerts/i)).not.toBeInTheDocument()
    })
  })
})
