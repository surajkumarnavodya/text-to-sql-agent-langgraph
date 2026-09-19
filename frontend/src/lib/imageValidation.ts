/** Client-side image upload validation. This is a UX convenience layer
 * ONLY -- it rejects an obviously-wrong file early so the user doesn't
 * wait for a slow round-trip to find out, but it is NOT a security
 * boundary. There is currently no backend endpoint that accepts an image
 * as a chat attachment at all (confirmed against `api/schemas.py`'s
 * `AskRequest` and every `UploadFile` route in `api/`), so there is no
 * server-side re-validation to layer this on top of yet -- when a real
 * upload endpoint is built, it must independently re-validate everything
 * checked here (magic bytes, size, dimensions), exactly the way
 * `api/documents.py` already does for PDFs. See
 * docs/image-editing-architecture.md.
 */

export const ACCEPTED_IMAGE_MIME_TYPES = ['image/png', 'image/jpeg', 'image/webp', 'image/gif'] as const
export type AcceptedImageMimeType = (typeof ACCEPTED_IMAGE_MIME_TYPES)[number]

export const MAX_IMAGE_BYTES = 10 * 1024 * 1024 // 10 MB
export const MAX_IMAGE_DIMENSION_PX = 8000 // guards against a decompression-bomb-shaped image
export const MAX_ATTACHED_IMAGES = 4

// Magic-byte signatures for the accepted formats -- checked against the
// file's own leading bytes, never its extension or the browser-reported
// `File.type` alone (both are trivially spoofable: a renamed .exe with a
// .png extension still reports `File.type === ''` or whatever the
// attacker sets, and `File.type` itself is just an OS/browser MIME
// sniff of the extension in most cases, not the content).
const MAGIC_BYTES: { mime: AcceptedImageMimeType; signature: number[]; offset?: number }[] = [
  { mime: 'image/png', signature: [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a] },
  { mime: 'image/jpeg', signature: [0xff, 0xd8, 0xff] },
  { mime: 'image/gif', signature: [0x47, 0x49, 0x46, 0x38] },
  // WEBP: 'RIFF' .... 'WEBP' -- two non-contiguous signatures, checked
  // specially below rather than forced into this table's simple shape.
]

export interface ImageValidationResult {
  ok: boolean
  /** Translation key for the human-readable reason, when ok is false. */
  errorKey?: string
  detectedMimeType?: AcceptedImageMimeType
  width?: number
  height?: number
}

async function sniffMimeType(file: File): Promise<AcceptedImageMimeType | null> {
  // Standard, real-browser File/Blob API. jsdom's own File/Blob
  // implementation (this project's test environment) doesn't implement
  // `.arrayBuffer()` at all -- worked around with a `src/test/setup.ts`
  // polyfill (FileReader-based) rather than here, so this function stays
  // exactly what a real browser needs, uncomplicated by a test-only gap.
  const head = new Uint8Array(await file.slice(0, 16).arrayBuffer())
  for (const { mime, signature } of MAGIC_BYTES) {
    if (signature.every((byte, index) => head[index] === byte)) return mime
  }
  // WEBP: RIFF????WEBP
  if (
    head[0] === 0x52 &&
    head[1] === 0x49 &&
    head[2] === 0x46 &&
    head[3] === 0x46 &&
    head[8] === 0x57 &&
    head[9] === 0x45 &&
    head[10] === 0x42 &&
    head[11] === 0x50
  ) {
    return 'image/webp'
  }
  return null
}

function readImageDimensions(file: File): Promise<{ width: number; height: number } | null> {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file)
    const img = new Image()
    img.onload = () => {
      URL.revokeObjectURL(url)
      resolve({ width: img.naturalWidth, height: img.naturalHeight })
    }
    img.onerror = () => {
      URL.revokeObjectURL(url)
      resolve(null)
    }
    img.src = url
  })
}

/** Validates one candidate image file. Checks, in order: size, real
 * content-sniffed MIME type (never the extension or `File.type` alone),
 * and that it actually decodes as an image within a sane dimension cap
 * (a file that passes the magic-byte check but fails to decode, or
 * decodes to an absurd resolution, is rejected too -- a magic-byte match
 * alone doesn't prove the rest of the file is well-formed). */
export async function validateImageFile(file: File): Promise<ImageValidationResult> {
  if (file.size === 0) return { ok: false, errorKey: 'image.errorEmpty' }
  if (file.size > MAX_IMAGE_BYTES) return { ok: false, errorKey: 'image.errorTooLarge' }

  const detectedMimeType = await sniffMimeType(file)
  if (!detectedMimeType) return { ok: false, errorKey: 'image.errorUnsupportedType' }

  const dimensions = await readImageDimensions(file)
  if (!dimensions) return { ok: false, errorKey: 'image.errorCorrupt' }
  if (dimensions.width > MAX_IMAGE_DIMENSION_PX || dimensions.height > MAX_IMAGE_DIMENSION_PX) {
    return { ok: false, errorKey: 'image.errorDimensionsTooLarge' }
  }

  return { ok: true, detectedMimeType, width: dimensions.width, height: dimensions.height }
}

export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}
