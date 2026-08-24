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
})
