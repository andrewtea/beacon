import react from '@vitejs/plugin-react'
import { VitePWA } from 'vite-plugin-pwa'
import { defineConfig } from 'vitest/config'

// The FastAPI server in ../server. Every server route lives under /api, so the
// built app and the API can share one origin in production.
const apiProxy = {
  '/api': process.env.BEACON_API_URL ?? 'http://127.0.0.1:8000',
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: 'autoUpdate',
      includeAssets: ['favicon.svg', 'apple-touch-icon.png'],
      manifest: {
        name: 'Beacon',
        short_name: 'Beacon',
        description:
          "Tap What's that? when you hear a siren or see a helicopter, and Beacon says what is going on, with a source on every claim.",
        theme_color: '#0b1f33',
        background_color: '#0b1f33',
        display: 'standalone',
        start_url: '/',
        icons: [
          { src: 'pwa-192x192.png', sizes: '192x192', type: 'image/png' },
          { src: 'pwa-512x512.png', sizes: '512x512', type: 'image/png' },
          {
            src: 'maskable-icon-512x512.png',
            sizes: '512x512',
            type: 'image/png',
            purpose: 'maskable',
          },
        ],
      },
      workbox: {
        // API requests must reach the server, never the cached app shell.
        // Workbox tests the path plus the query string, so this also covers
        // /api and /api?x.
        navigateFallbackDenylist: [/^\/api(?:[/?]|$)/],
      },
    }),
  ],
  server: { proxy: apiProxy },
  preview: { proxy: apiProxy },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    unstubGlobals: true,
  },
})
