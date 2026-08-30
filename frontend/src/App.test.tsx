import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import App from './App'

describe('App', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
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
})
