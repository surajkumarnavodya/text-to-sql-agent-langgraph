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
  '/attachments',
  '/execute',
  '/documents',
  '/schema',
  '/feedback',
  '/health',
  '/media',
  '/generate',
  '/voice',
  '/search',
  '/auth',
  '/conversations',
  '/chat',
  // Secure conversation sharing (api/shares.py) -- deliberately its own
  // path prefix, distinct from the frontend's own client-side `/shared/:ref`
  // viewer *page* route, so this backend JSON API and that SPA page never
  // collide at the same URL. Explicit NetworkOnly here is not optional the
  // way it is for an ordinary GET: a cached shared-conversation response
  // would be exactly the kind of private-content cache leak this feature's
  // own security spec calls out by name.
  '/share-view',
  '/share-invitations',
  // Client-database onboarding (api/onboarding.py, Prompt 08/26).
  '/onboarding',
  // Tenant-aware semantic catalog (api/semantic_catalog.py, Prompt 09/27) --
  // was missing here before Prompt 27, same real dev-only gap `/onboarding`
  // had before Prompt 26 found and fixed it (`npm run dev` would 404 every
  // call despite `npm run build`'s production output working fine).
  '/semantic-catalog',
  // Global platform admin dashboard (api/platform_admin.py, Prompt 28) --
  // the same recurring dev-proxy gap found and fixed for both prompts
  // immediately before this one; added here up front rather than found
  // the hard way again.
  '/platform-admin',
  // Tenant/client admin dashboard (api/tenant_admin.py, Prompt 29).
  '/tenant-admin',
  // Recommendation governance API (api/recommendation_governance.py, Prompt
  // 18/31) -- missing from this list before Prompt 32, the same recurring
  // dev-only gap Prompts 26/27/28/29 each found. The React page itself lives at
  // `/recommended-actions` (a path no API route uses), not here.
  '/recommendations',
  // Role-based navigation (api/navigation.py, Prompt 32).
  '/navigation',
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
      BACKEND_ROUTES.map((route) => [
        route,
        {
          target: 'http://localhost:8000',
          changeOrigin: true,
          // A browser page load (Accept: text/html) of a route that is ALSO a
          // React page path -- e.g. `/platform-admin` or `/tenant-admin` -- must
          // reach the app, not the API. Prefix proxying would otherwise send the
          // reload to the backend, which has no such page and answers 404 JSON.
          // API calls (fetch/XHR, which never send text/html) still proxy.
          bypass: (req) => (req.headers.accept?.includes('text/html') ? req.url : undefined),
        },
      ]),
    ),
  },
})
