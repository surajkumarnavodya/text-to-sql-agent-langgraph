import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'
import '@testing-library/jest-dom/vitest'

// Real i18next init (same module App.tsx's import chain triggers), so any
// component under test that calls useTranslation() renders real English
// strings instead of raw translation keys or a "not initialized" warning --
// without this, every text assertion in a component test would need its
// own ad hoc i18next mock.
import '@/i18n'

// `globals: true` is deliberately not set in vitest.config.ts (tests import
// describe/it/expect explicitly instead), which means @testing-library/
// react's own implicit afterEach(cleanup) registration never fires -- it
// only self-registers when it detects a global test-framework hook. Without
// this, DOM nodes from one test leak into the next render, and
// `getByRole`/`getByText` start throwing "multiple elements found."
afterEach(() => {
  cleanup()
})

// jsdom's own File/Blob implementation in this environment has no
// `.arrayBuffer()` (confirmed: only `.slice()` exists) -- real browsers
// have had this for years, so application code (lib/imageValidation.ts)
// is written against the real, standard API rather than working around a
// test-only gap. Polyfilled here instead, via the much older (and, in
// jsdom, actually implemented) FileReader API, so that real code doesn't
// need to know its test environment is incomplete.
if (typeof Blob !== 'undefined' && !Blob.prototype.arrayBuffer) {
  Blob.prototype.arrayBuffer = function arrayBufferPolyfill(): Promise<ArrayBuffer> {
    return new Promise((resolve, reject) => {
      const reader = new FileReader()
      reader.onload = () => resolve(reader.result as ArrayBuffer)
      reader.onerror = () => reject(reader.error)
      reader.readAsArrayBuffer(this)
    })
  }
}

// matchMedia is used by useTheme/usePwaInstall-style hooks that check
// `prefers-color-scheme`/`prefers-reduced-motion` -- jsdom doesn't implement
// it, so components that call it would otherwise throw in every test.
if (!window.matchMedia) {
  window.matchMedia = (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })
}
