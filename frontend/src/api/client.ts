export interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
}

export interface ChatResponse {
  reply: string
}

export interface UserLocation {
  lat: number
  lon: number
}

const ANONYMOUS_ID_KEY = 'imts_anonymous_id'
const LOCATION_PHRASES = ['here', 'my location', 'near me', 'current location', 'where i am']
const LOCATION_STALE_AFTER_MS = 5 * 60 * 1000

let cachedLocation: UserLocation | null = null
let cachedLocationAt: number | null = null

// Internal test helper — resets the module-level location cache.
// Not exported in the public API; used only in tests.
export function _resetLocationCache() {
  cachedLocation = null
  cachedLocationAt = null
}

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

export function shouldRequestLocation(message: string): boolean {
  const lower = message.toLowerCase()
  return LOCATION_PHRASES.some((phrase) => lower.includes(phrase))
}

function isLocationStale(): boolean {
  return cachedLocationAt === null || Date.now() - cachedLocationAt > LOCATION_STALE_AFTER_MS
}

function requestLocation(): Promise<UserLocation | null> {
  return new Promise((resolve) => {
    if (!navigator.geolocation) {
      resolve(null)
      return
    }
    navigator.geolocation.getCurrentPosition(
      (position) => resolve({ lat: position.coords.latitude, lon: position.coords.longitude }),
      () => resolve(null), // denied/unavailable -- design doc: no user_location sent, agent asks instead
      // enableHighAccuracy (2026-11-16, real bug): without this, the
      // browser defaults to fast, low-power WiFi/cell-tower triangulation,
      // which can misplace a device by a mile or more in exactly the kind
      // of dense, river-adjacent area this product's beachhead users are
      // in (Roosevelt Island) -- confirmed live: a query from Roosevelt
      // Island produced routes starting "walk to 96 St"/"walk to 86 St",
      // ~1.5mi away on the Manhattan side, while plan_route given the
      // REAL Roosevelt Island coordinates correctly returns the fast
      // direct subway->Q70-SBS route. Forcing GPS (when available) is a
      // zero-cost, one-flag fix for the actual root cause -- the routing
      // engine itself was never wrong, only the location fed into it.
      { enableHighAccuracy: true }
    )
  })
}

export async function sendChatMessage(
  message: string,
  conversationHistory: ChatMessage[]
): Promise<ChatResponse> {
  if (shouldRequestLocation(message) && isLocationStale()) {
    const result = await requestLocation()
    if (result !== null) {
      cachedLocation = result
      cachedLocationAt = Date.now()
    }
  }

  const response = await fetch('/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      message,
      conversation_history: conversationHistory,
      anonymous_id: getOrCreateAnonymousId(),
      user_location: isLocationStale() ? null : cachedLocation,
    }),
  })
  if (!response.ok) {
    throw new Error(`Chat request failed with status ${response.status}`)
  }
  return response.json()
}
