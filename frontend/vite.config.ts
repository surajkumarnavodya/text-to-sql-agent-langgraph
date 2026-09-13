import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'
import { VitePWA } from 'vite-plugin-pwa'

// Dev-only proxy to the FastAPI backend (api/main.py) so the browser never
// needs CORS at all in development -- the production build is served as
// static files by that same FastAPI process at the same root paths (see
// api/main.py's StaticFiles mount), so the frontend calls plain paths like
// `/ask`/`/execute` in both modes with no `/api` prefix to keep in sync.
// Listed explicitly (rather than proxying everything) so client-side
// react-router routes never accidentally get proxied to the backend.
const BACKEND_ROUTES = [
  '/ask',
  '/execute',
  '/documents',
  '/schema',
  '/feedback',
  '/health',
  '/media',
  '/generate',
  '/voice',
]

export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
    VitePWA({
      registerType: 'autoUpdate',
      // Precaches only the built app shell (JS/CSS/icons/index.html) --
      // never an API response. This app's entire value is a *live* answer
      // from a real database/LLM; a cached /ask response would silently
      // show a stale or wrong answer, which is worse than the request
      // simply failing offline. See workbox.runtimeCaching below for the
      // explicit NetworkOnly guard on every backend route.
      includeAssets: ['icon-192.png', 'icon-512.png', 'icon-512-maskable.png', 'apple-touch-icon.png'],
      manifest: {
        name: 'Text-to-SQL Dashboard',
        short_name: 'Text-to-SQL',
        description:
          'Ask questions about your data in plain English and get validated, read-only SQL, results, and charts.',
        theme_color: '#4f46e5',
        background_color: '#f8fafc',
        display: 'standalone',
        start_url: '/',
        scope: '/',
        icons: [
          { src: '/icon-192.png', sizes: '192x192', type: 'image/png', purpose: 'any' },
          { src: '/icon-512.png', sizes: '512x512', type: 'image/png', purpose: 'any' },
          {
            src: '/icon-512-maskable.png',
            sizes: '512x512',
            type: 'image/png',
            purpose: 'maskable',
          },
        ],
      },
      workbox: {
        // Explicit NetworkOnly for every backend route -- belt-and-braces
        // alongside "never precached" above: even a future runtime-caching
        // rule added by mistake for a broader glob can't accidentally
        // start serving a stale agent answer, document list, or schema
        // snapshot while "offline-looking" but actually wrong.
        runtimeCaching: BACKEND_ROUTES.map((route) => ({
          urlPattern: new RegExp(`^${route}`),
          handler: 'NetworkOnly' as const,
        })),
      },
      devOptions: {
        // Lets `npm run dev` also register a service worker, so the
        // install prompt/PWA behavior can be checked without a full build.
        enabled: true,
        type: 'module',
      },
    }),
  ],
  resolve: {
    alias: {
      '@': `${import.meta.dirname}/src`,
    },
  },
  server: {
    proxy: Object.fromEntries(
      BACKEND_ROUTES.map((route) => [route, { target: 'http://localhost:8000', changeOrigin: true }]),
    ),
  },
})
