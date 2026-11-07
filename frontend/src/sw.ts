/// <reference lib="webworker" />
// Custom service worker (2026-11-06) -- switched from vite-plugin-pwa's
// generateSW strategy to injectManifest specifically to add the push/
// notificationclick handlers below; generateSW only emits a precaching
// worker with no hook for custom runtime event listeners.
import { precacheAndRoute } from 'workbox-precaching'

declare let self: ServiceWorkerGlobalScope

precacheAndRoute(self.__WB_MANIFEST)

self.addEventListener('push', (event: PushEvent) => {
  const data = event.data ? event.data.json() : { title: 'Is my train screwed?', body: '' }
  event.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      icon: '/icons/icon-192.png',
      badge: '/icons/icon-192.png',
    })
  )
})

self.addEventListener('notificationclick', (event: NotificationEvent) => {
  event.notification.close()
  event.waitUntil(self.clients.openWindow('/'))
})
