import { describe, it, expect, vi, beforeEach } from 'vitest'
import { getOrCreateAnonymousId, sendChatMessage } from './client'

describe('getOrCreateAnonymousId', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.restoreAllMocks()
  })

  it('generates and persists a new id on first call (empty localStorage)', () => {
    expect(localStorage.getItem('imts_anonymous_id')).toBeNull()
    const id = getOrCreateAnonymousId()
    expect(id).toMatch(/^[0-9a-f-]{36}$/i)
    expect(localStorage.getItem('imts_anonymous_id')).toBe(id)
  })

  it('returns the same id on a second call instead of regenerating', () => {
    const first = getOrCreateAnonymousId()
    const second = getOrCreateAnonymousId()
    expect(second).toBe(first)
  })

  it('falls back to a valid id without crashing when localStorage throws', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('The operation is insecure.', 'SecurityError')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('The operation is insecure.', 'SecurityError')
    })

    const id = getOrCreateAnonymousId()
    expect(id).toMatch(/^[0-9a-f-]{36}$/i)
  })
})

describe('sendChatMessage', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.restoreAllMocks()
  })

  it('includes anonymous_id in the POST body', async () => {
    const fetchMock = vi.spyOn(global, 'fetch').mockResolvedValue({
      ok: true,
      json: async () => ({ reply: 'ok' }),
    } as Response)

    const expectedId = getOrCreateAnonymousId()
    await sendChatMessage('hello', [])

    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [, options] = fetchMock.mock.calls[0]
    const body = JSON.parse(options!.body as string)
    expect(body).toEqual({
      message: 'hello',
      conversation_history: [],
      anonymous_id: expectedId,
    })
  })
})
