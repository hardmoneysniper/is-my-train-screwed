/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { VitePWA } from 'vite-plugin-pwa'

export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: 'autoUpdate',
      // injectManifest (not the default generateSW) because src/sw.ts adds
      // its own push/notificationclick handlers (2026-11-06) -- generateSW
      // only ever emits a plain precaching worker with no hook for custom
      // runtime listeners.
      strategies: 'injectManifest',
      srcDir: 'src',
      filename: 'sw.ts',
      manifest: {
        name: 'Is My Train Screwed?',
        short_name: 'TrainScrewed',
        start_url: '/',
        display: 'standalone',
        background_color: '#ffffff',
        theme_color: '#0096ff',
        // Real logo (2026-11-06) -- replaced the 2026-10-11 white-box
        // testing placeholder. Source is the project's logo.png, resized
        // to both required sizes (no separate generation script needed,
        // a one-off PIL resize was simpler for a single source image).
        icons: [
          { src: '/icons/icon-192.png', sizes: '192x192', type: 'image/png' },
          { src: '/icons/icon-512.png', sizes: '512x512', type: 'image/png' },
        ],
      },
    }),
  ],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: './src/setupTests.ts',
  },
})
