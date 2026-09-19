import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { formatFileSize, MAX_IMAGE_BYTES, MAX_IMAGE_DIMENSION_PX, validateImageFile } from './imageValidation'

const PNG_SIGNATURE = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]
const JPEG_SIGNATURE = [0xff, 0xd8, 0xff]

function makeFile(bytes: number[], name = 'photo.png', type = 'image/png'): File {
  return new File([new Uint8Array(bytes)], name, { type })
}

/** jsdom doesn't actually decode images -- Image.onload/onerror never fire
 * on their own. Stub `Image` so validateImageFile's dimension check can be
 * exercised deterministically in both the success and failure directions. */
function stubImageLoad(outcome: { width: number; height: number } | 'error') {
  class FakeImage {
    onload: (() => void) | null = null
    onerror: (() => void) | null = null
    naturalWidth = 0
    naturalHeight = 0
    set src(_value: string) {
      queueMicrotask(() => {
        if (outcome === 'error') {
          this.onerror?.()
        } else {
          this.naturalWidth = outcome.width
          this.naturalHeight = outcome.height
          this.onload?.()
        }
      })
    }
  }
  vi.stubGlobal('Image', FakeImage)
}

describe('validateImageFile', () => {
  beforeEach(() => {
    vi.stubGlobal('URL', { ...URL, createObjectURL: vi.fn(() => 'blob:mock'), revokeObjectURL: vi.fn() })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('rejects an empty file', async () => {
    const file = makeFile([])
    const result = await validateImageFile(file)
    expect(result.ok).toBe(false)
    expect(result.errorKey).toBe('image.errorEmpty')
  })

  it('rejects a file larger than the size cap', async () => {
    const file = new File([new Uint8Array(MAX_IMAGE_BYTES + 1)], 'big.png', { type: 'image/png' })
    const result = await validateImageFile(file)
    expect(result.ok).toBe(false)
    expect(result.errorKey).toBe('image.errorTooLarge')
  })

  it('rejects content that does not match any accepted magic-byte signature, regardless of extension/File.type', async () => {
    // A renamed non-image (e.g. an executable) claiming to be a PNG via
    // both its extension and its (spoofable) File.type -- the real
    // content-sniff must still catch it.
    const file = makeFile([0x4d, 0x5a, 0x90, 0x00], 'totally-a-photo.png', 'image/png')
    const result = await validateImageFile(file)
    expect(result.ok).toBe(false)
    expect(result.errorKey).toBe('image.errorUnsupportedType')
  })

  it('accepts a real PNG signature and reports its decoded dimensions', async () => {
    stubImageLoad({ width: 800, height: 600 })
    const file = makeFile(PNG_SIGNATURE)
    const result = await validateImageFile(file)
    expect(result.ok).toBe(true)
    expect(result.detectedMimeType).toBe('image/png')
    expect(result.width).toBe(800)
    expect(result.height).toBe(600)
  })

  it('accepts a real JPEG signature', async () => {
    stubImageLoad({ width: 100, height: 100 })
    const file = makeFile(JPEG_SIGNATURE, 'photo.jpg', 'image/jpeg')
    const result = await validateImageFile(file)
    expect(result.ok).toBe(true)
    expect(result.detectedMimeType).toBe('image/jpeg')
  })

  it('rejects a file with a valid signature that fails to decode as an image', async () => {
    stubImageLoad('error')
    const file = makeFile(PNG_SIGNATURE)
    const result = await validateImageFile(file)
    expect(result.ok).toBe(false)
    expect(result.errorKey).toBe('image.errorCorrupt')
  })

  it('rejects an image whose dimensions exceed the cap', async () => {
    stubImageLoad({ width: MAX_IMAGE_DIMENSION_PX + 1, height: 100 })
    const file = makeFile(PNG_SIGNATURE)
    const result = await validateImageFile(file)
    expect(result.ok).toBe(false)
    expect(result.errorKey).toBe('image.errorDimensionsTooLarge')
  })
})

describe('formatFileSize', () => {
  it('formats bytes, kilobytes, and megabytes', () => {
    expect(formatFileSize(500)).toBe('500 B')
    expect(formatFileSize(2048)).toBe('2.0 KB')
    expect(formatFileSize(5 * 1024 * 1024)).toBe('5.0 MB')
  })
})
