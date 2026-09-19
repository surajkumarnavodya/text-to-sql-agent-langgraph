import { mergeConfig } from 'vite'
import { defineConfig as defineVitestConfig } from 'vitest/config'
import viteConfig from './vite.config'

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
    },
  }),
)
