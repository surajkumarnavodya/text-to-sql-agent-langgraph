import { mergeConfig } from 'vite'
import { defineConfig as defineVitestConfig } from 'vitest/config'
import viteConfig from './vite.config.ts'

// A separate config file (rather than a `test` block bolted onto
// vite.config.ts) so the production build config stays exactly what it was
// -- no vitest-only types/behavior leak into `vite build`/`vite preview`.
// Reuses the real app config (the `@` alias, React plugin) via `mergeConfig`
// so component tests resolve imports identically to the built app.
export default mergeConfig(
  viteConfig,
  defineVitestConfig({
    test: {
      environment: 'jsdom',
      setupFiles: ['./src/test/setup.ts'],
      css: true,
      restoreMocks: true,
      // Vitest 4 changed `restoreMocks`/`vi.restoreAllMocks()` to only
      // restore true `vi.spyOn()` spies to their original implementation --
      // it no longer clears call history on a plain `vi.fn()` created inside
      // a `vi.mock()` factory (there's no "original" to restore to), whereas
      // Vitest 3 happened to also reset those. Several test files rely on a
      // module-level `vi.fn()` mock being call-history-free at the start of
      // each test; `clearMocks` restores that guarantee explicitly rather
      // than depending on `restoreMocks`' version-specific side effect.
      clearMocks: true,
    },
  }),
)
