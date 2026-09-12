# Text-to-SQL Dashboard — React frontend

A modern, installable (PWA) replacement for the Streamlit chat UI
(`ui/app.py`), talking to the same FastAPI backend (`api/main.py`) that
already served it. Streamlit is kept running side by side until this
frontend has full verified feature parity (see the repo's `CLAUDE.md`) —
this is not yet the only UI the project ships.

## Stack

- **Vite + React 19 + TypeScript**
- **Tailwind CSS v4** (`@tailwindcss/vite`, CSS-variable-driven design
  tokens — see `src/index.css`/`src/lib/theme.ts`) with a small set of
  hand-built, Radix-based UI primitives (`src/components/ui/`) rather than
  a full component-library dependency
- **Zustand** for client state (`src/store/`) — settings (theme/accent/
  font/language/sidebar) persisted to `localStorage`; chat/query history is
  session-only, matching the original Streamlit app's own behavior
- **TanStack Query** for server state, **TanStack Table** for the results
  datatable (sorting/filtering/pagination)
- **Chart.js** (`react-chartjs-2`) for result charts — the backend
  (`agent/result_charting.py`) still decides chart type/data; the frontend
  only renders it
- **react-i18next** for i18n (`src/i18n/locales/{en,es,fr,hi,mr}`)
- **react-markdown** + `remark-gfm` + `@tailwindcss/typography` for
  rendering LLM-generated answers (bold/lists/clickable links) safely (no
  raw HTML passthrough — retrieved web/document content is untrusted)
- **vite-plugin-pwa** for installability — see its config in
  `vite.config.ts` for why API routes are explicitly `NetworkOnly`

## Running

```bash
npm install
npm run dev      # Vite dev server, proxies /ask, /execute, /documents,
                  # /schema, /feedback, /health to localhost:8000
npm run build    # outputs to dist/ -- api/main.py serves this directly
                  # in production (same origin, no CORS needed)
npm run lint     # oxlint
```

The backend (`uvicorn api.main:app`) must be running for anything beyond
the empty shell to work. See the repo root `README.md`/`CLAUDE.md` for
backend setup (`.env`, Ollama, database connections).
