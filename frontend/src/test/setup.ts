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

// ResizeObserver is used internally by Chart.js (`chart.js`'s DomPlatform
// binds one to track canvas-container attach/detach and size changes) --
// jsdom doesn't implement it, so any test that re-renders an already-mounted
// chart (a type/option change, not just the initial mount) would otherwise
// throw deep inside Chart.js's own resize-binding code. Real browsers have
// always had this, so ResultChart.tsx is written against the real API; this
// is a minimal stub, not a behavior mock -- it only needs to exist.
if (typeof window.ResizeObserver === 'undefined') {
  window.ResizeObserver = class ResizeObserverStub {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
}

// jsdom's `HTMLCanvasElement.getContext('2d')` is unimplemented (confirmed:
// logs "Not implemented" and returns `undefined`). Chart.js treats a falsy
// context not as "no-op the draw calls" but as a fully-failed construction
// (`this.canvas`/`this._responsiveListeners` stay `undefined` forever, and
// `_initialize()`/`bindEvents()` are skipped entirely -- see chart.js's own
// `Chart` constructor) -- a half-built instance that then crashes deep
// inside its own attach/detach resize-bind logic
// (`Cannot read properties of null (reading 'ownerDocument')`) the moment
// anything calls `.update()` on it, e.g. a chart-type/option change. A
// minimal fake 2D context (every method no-ops; unknown members are stubbed
// on first access) lets Chart.js's real construction/lifecycle path run
// instead of aborting early into that broken state -- this is a jsdom
// capability gap being polyfilled, the same category as the ResizeObserver
// stub above and the deliberate choice (documented in
// ResultChart.test.tsx) not to pull in the full `canvas` npm package just
// for pixel output nobody asserts on here.
if (typeof HTMLCanvasElement !== 'undefined') {
  const fakeContext2d = (canvas: HTMLCanvasElement) =>
    new Proxy(
      { canvas } as unknown as CanvasRenderingContext2D,
      {
        get(target, prop) {
          if (prop in target) return (target as unknown as Record<string | symbol, unknown>)[prop]
          if (prop === 'measureText') return () => ({ width: 0 })
          if (prop === 'createLinearGradient' || prop === 'createRadialGradient') {
            return () => ({ addColorStop: () => {} })
          }
          if (prop === 'getImageData') return () => ({ data: [] })
          if (prop === 'isPointInPath' || prop === 'isPointInStroke') return () => false
          return () => {}
        },
        set(target, prop, value) {
          ;(target as unknown as Record<string | symbol, unknown>)[prop] = value
          return true
        },
      },
    )

  HTMLCanvasElement.prototype.getContext = function fakeGetContext(
    this: HTMLCanvasElement,
    contextId: string,
  ) {
    return contextId === '2d' ? fakeContext2d(this) : null
  } as typeof HTMLCanvasElement.prototype.getContext

  // jsdom's `HTMLCanvasElement.toDataURL()` is also unimplemented -- unlike
  // `getContext` above (which jsdom at least calls out via a console
  // warning), this one silently returns `undefined` with no error at all.
  // `ImageEditor.tsx`'s Konva `Stage.toDataURL()` (used to export the
  // current canvas/mask for every AI-edit/blur/extract-text/download flow)
  // delegates to this native method at the end of its own rendering
  // pipeline, so without a stub every one of those flows silently no-ops
  // (`flattenToDataUrl()`/`flattenMaskOnly()` return `undefined`) with no
  // exception to catch -- a real capability gap, not a behavior mock. A
  // fixed, valid 1x1 transparent PNG data URL is enough for every test here,
  // since nothing in this test suite asserts on actual exported pixel data.
  HTMLCanvasElement.prototype.toDataURL = function fakeToDataUrl(): string {
    return 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='
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
