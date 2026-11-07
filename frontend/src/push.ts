// Web Push subscription flow (2026-11-06) -- requests notification
// permission and subscribes this browser against the backend's VAPID
// public key, so app/trip_monitor.py can push real-time reroute alerts
// straight to this device without /chat needing to be open. Feature-
// detected throughout: iOS Safari outside an installed, standalone PWA
// (iOS 16.4+) has no PushManager at all -- this silently no-ops there,
// which is fine, since the install-prompt bubble in App.tsx is what's
// steering those riders toward installing first.

function urlBase64ToUint8Array(base64: string): Uint8Array<ArrayBuffer> {
  const padding = '='.repeat((4 - (base64.length % 4)) % 4)
  const base64Safe = (base64 + padding).replace(/-/g, '+').replace(/_/g, '/')
  const raw = window.atob(base64Safe)
  // new Uint8Array(length) (not Uint8Array.from, whose TS type widens to
  // the generic ArrayBufferLike-backed variant) so this is concretely
  // ArrayBuffer-backed -- PushManager.subscribe's applicationServerKey
  // type requires that exact specialization, not the wider one.
  const bytes = new Uint8Array(raw.length)
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i)
  return bytes
}

export async function subscribeToPush(anonymousId: string): Promise<void> {
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) return
  if (Notification.permission === 'denied') return
  if (Notification.permission === 'default') {
    const permission = await Notification.requestPermission()
    if (permission !== 'granted') return
  }

  try {
    const registration = await navigator.serviceWorker.ready
    let subscription = await registration.pushManager.getSubscription()
    if (!subscription) {
      const res = await fetch('/push/public-key')
      const { public_key } = await res.json()
      if (!public_key) return // not configured server-side -- nothing to subscribe against
      subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(public_key),
      })
    }
    await fetch('/push/subscribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ anonymous_id: anonymousId, subscription: subscription.toJSON() }),
    })
  } catch {
    // Best-effort enhancement -- a rider who declines/fails push still
    // gets every notification the existing pull-based /chat path already
    // delivers, so a failure here must never break the chat flow itself.
  }
}
