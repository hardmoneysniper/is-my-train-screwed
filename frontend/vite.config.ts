/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { VitePWA } from 'vite-plugin-pwa'

export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: 'autoUpdate',
      manifest: {
        name: 'Is My Train Screwed?',
        short_name: 'TrainScrewed',
        start_url: '/',
        display: 'standalone',
        background_color: '#ffffff',
        theme_color: '#0096ff',
        // Placeholder white-box icons (2026-10-11, user: "use a white box
        // for the pwa logo since it is testing right now") -- swap for
        // real logo assets before a real launch, see
        // scripts/_generate_placeholder_icons.py for how these were made.
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
