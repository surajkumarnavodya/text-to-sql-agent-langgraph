# Text-to-SQL Dashboard — React frontend

A modern, installable (PWA) dashboard talking to the FastAPI backend
(`api/main.py`), which also serves this app's own build output in
production. This is the only UI the project ships — an earlier Streamlit
app was removed once this dashboard reached full feature parity with it
(see the repo's `CLAUDE.md`).

## Stack

- **Vite + React 19 + TypeScript**
- **Tailwind CSS v4** (`@tailwindcss/vite`, CSS-variable-driven design
  tokens — see `src/index.css`/`src/lib/theme.ts`) with a small set of
  hand-built, Radix-based UI primitives (`src/components/ui/`) rather than
  a full component-library dependency
- **Zustand** for client state (`src/store/`) — settings (theme/accent/
  font/language) persisted to `localStorage`; chat/query history
  (`chatStore.ts`'s `conversations`) is session-only, cleared on a full
  page reload by design, not a regression
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
                  # /schema, /feedback, /health, /media, /generate to
                  # localhost:8000 (see vite.config.ts's BACKEND_ROUTES)
npm run build    # outputs to dist/ -- api/main.py serves this directly
                  # in production (same origin, no CORS needed)
npm run lint     # oxlint
```

The backend (`uvicorn api.main:app`) must be running for anything beyond
the empty shell to work. See the repo root `README.md`/`CLAUDE.md` for
backend setup (`.env`, Ollama, database connections).
