import { describe, it, expect, vi, beforeEach } from 'vitest'
import { getOrCreateAnonymousId, sendChatMessage, shouldRequestLocation, _resetLocationCache } from './client'

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

describe('shouldRequestLocation', () => {
  it('matches known location phrases case-insensitively', () => {
    expect(shouldRequestLocation('get me home from here')).toBe(true)
    expect(shouldRequestLocation('What is NEAR ME right now')).toBe(true)
    expect(shouldRequestLocation('take me to my location please')).toBe(true)
    expect(shouldRequestLocation('take me home from my location')).toBe(true)
    expect(shouldRequestLocation('plan a trip to Roosevelt Island')).toBe(false)
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
      user_location: null,
    })
  })
})

describe('sendChatMessage with location', () => {
  const mockGeolocation = {
    getCurrentPosition: vi.fn(),
  }

  beforeEach(() => {
    _resetLocationCache()
    mockGeolocation.getCurrentPosition.mockClear()
    vi.stubGlobal('navigator', { geolocation: mockGeolocation })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ reply: 'ok' }),
    }))
  })

  it('attaches user_location after a successful geolocation request triggered by a keyword match', async () => {
    mockGeolocation.getCurrentPosition.mockImplementation((success) => {
      success({ coords: { latitude: 40.7597, longitude: -73.9532 } })
    })

    await sendChatMessage('get me home from here', [])

    const callBody = JSON.parse((fetch as any).mock.calls[0][1].body)
    expect(callBody.user_location).toEqual({ lat: 40.7597, lon: -73.9532 })
  })

  it('sends user_location: null when the message has no location keyword and none was cached yet', async () => {
    await sendChatMessage('plan a trip to Roosevelt Island', [])

    const callBody = JSON.parse((fetch as any).mock.calls[0][1].body)
    expect(callBody.user_location).toBeNull()
    expect(mockGeolocation.getCurrentPosition).not.toHaveBeenCalled()
  })

  it('re-requests geolocation when the cached value is older than 5 minutes', async () => {
    let callCount = 0
    mockGeolocation.getCurrentPosition.mockImplementation((success) => {
      callCount += 1
      success({ coords: { latitude: 40.0 + callCount, longitude: -73.0 } })
    })
    vi.useFakeTimers()

    await sendChatMessage('get me home from here', [])
    expect(mockGeolocation.getCurrentPosition).toHaveBeenCalledTimes(1)

    vi.advanceTimersByTime(6 * 60 * 1000) // 6 minutes -- past the 5-minute staleness window

    await sendChatMessage('get me home from here', [])
    expect(mockGeolocation.getCurrentPosition).toHaveBeenCalledTimes(2)

    const secondCallBody = JSON.parse((fetch as any).mock.calls[1][1].body)
    expect(secondCallBody.user_location).toEqual({ lat: 42.0, lon: -73.0 }) // the re-requested value, not the stale cached one

    vi.useRealTimers()
  })

  it('does not re-request geolocation within the 5-minute window', async () => {
    mockGeolocation.getCurrentPosition.mockImplementation((success) => {
      success({ coords: { latitude: 40.7597, longitude: -73.9532 } })
    })
    vi.useFakeTimers()

    await sendChatMessage('get me home from here', [])
    vi.advanceTimersByTime(2 * 60 * 1000) // 2 minutes -- still within the window
    await sendChatMessage('take me from here to work', [])

    expect(mockGeolocation.getCurrentPosition).toHaveBeenCalledTimes(1) // not called again

    vi.useRealTimers()
  })
})
