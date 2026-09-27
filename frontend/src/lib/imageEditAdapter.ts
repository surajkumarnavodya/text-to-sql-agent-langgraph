/** The seam between the image editor UI and whatever actually performs an
 * AI-guided (generative) edit. See docs/image-editing-architecture.md for
 * the full history -- this module used to be a deliberate, permanent stub
 * (`AiGuidedEditAdapter.editWithInstruction` always rejected with
 * `AiEditNotConfiguredError`, since no backend endpoint existed at all).
 * That backend now exists (`POST /attachments/{id}/ai-edit`,
 * `attachments/ai_edit.py` + `media_gen/image_edit_provider.py` --
 * IMA Studio's `image_to_image` task category, live-verified against a
 * real account on 2026-09-27) -- this adapter calls it for real.
 *
 * Two implementations still exist, with the same honesty split as before:
 * - `LocalCanvasAdapter` (unchanged, not in this file): real work, entirely
 *   in the browser (crop, rotate, flip, draw, annotate, resize, blur, OCR)
 *   -- no network call for any of that, and the UI never claims otherwise.
 * - `AiGuidedEditAdapter` (this file): does real work too, now -- it
 *   uploads the current edited canvas as a fresh, transient attachment
 *   (`POST /attachments/upload`, the exact same validated/scanned/stored
 *   path any other chat attachment goes through), then calls the real
 *   AI-edit endpoint. **Never uses the original file** -- the current
 *   canvas export is always what's uploaded, so a crop/rotate/annotation
 *   the user made locally before clicking Generate is what the model
 *   actually sees, never a stale original (see this module's own
 *   `editWithInstruction` docstring).
 *
 * `AiEditNotConfiguredError` still exists and is still thrown for real --
 * now only when the server's own capability flag
 * (`AttachmentCapabilities.image_ai_editing`) is off, which `ImageEditor
 * .tsx` checks *before* ever calling this adapter (the honest "query a
 * capability endpoint, disable unavailable actions" pattern) -- calling
 * this adapter's `editWithInstruction` while the capability is off would
 * still fail cleanly (the backend itself also checks), but the UI should
 * never let that happen in the first place.
 */

import { aiEditAttachmentImage, ApiError, deleteAttachment, uploadAttachments } from './api'
import type { AiImageEditOperation } from './types'

export interface AiGuidedEditRequest {
  /** The current edited image, as a data URL (image/png) -- must be
   * captured fresh at Generate-click time (see `ImageEditor.tsx
   * ::handleAiGenerate`), never a stale reference to what was loaded when
   * the editor opened. */
  imageDataUrl: string
  /** A real, pixel-aligned mask PNG (data URL), exported at the *current
   * flattened canvas*'s own pixel dimensions -- see `ImageEditor.tsx
   * ::flattenMaskOnly`'s own docstring for how alignment is guaranteed.
   * `null` means "the whole image" (no painted region). */
  maskDataUrl: string | null
  /** e.g. "Remove the selected object." / "Replace the sky with a sunset." */
  instruction: string
  operation: AiImageEditOperation
}

export interface AiGuidedEditResult {
  imageDataUrl: string
  /** The new, server-stored derived attachment holding the edited result
   * -- usable for a follow-up question via `AskRequest.attachment_ids`
   * without re-uploading anything. */
  attachmentId: string
  provider: string | null
  model: string | null
  warnings: string[]
}

export class AiEditNotConfiguredError extends Error {
  constructor() {
    super(
      'AI-guided image editing is not configured on this server. Your local edits above are unaffected.',
    )
    this.name = 'AiEditNotConfiguredError'
  }
}

/** Thrown for an ordinary "the edit did not succeed" outcome -- a content-
 * policy rejection, a rate limit, a provider timeout/refusal, an empty
 * mask. `errorCode` lets the caller distinguish these without parsing
 * `message` text. Never thrown for a genuine network/transport failure --
 * that surfaces as a plain `ApiError` instead, from `request()` itself. */
export class AiEditFailedError extends Error {
  errorCode: string | null

  constructor(message: string, errorCode: string | null) {
    super(message)
    this.name = 'AiEditFailedError'
    this.errorCode = errorCode
  }
}

export interface ImageEditAdapter {
  editWithInstruction(request: AiGuidedEditRequest): Promise<AiGuidedEditResult>
}

function dataUrlToFile(dataUrl: string, filename: string): File {
  const [header, base64Data] = dataUrl.split(',')
  const mimeMatch = /data:([^;]+)/.exec(header)
  const mime = mimeMatch?.[1] ?? 'image/png'
  const binary = atob(base64Data)
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i)
  return new File([bytes], filename, { type: mime })
}

/** The adapter actually wired into the editor's UI. Real, end-to-end: an
 * upload, then a generative-edit call, both against this app's own
 * server. See this module's own docstring for the full design. */
export class AiGuidedEditAdapter implements ImageEditAdapter {
  async editWithInstruction(request: AiGuidedEditRequest): Promise<AiGuidedEditResult> {
    // Step 1: upload the *current* canvas export as a fresh, transient
    // attachment -- reuses the exact same validated/malware-scanned/
    // stored path any other chat attachment goes through (never a
    // separate, unaudited channel for this feature specifically).
    const sourceFile = dataUrlToFile(request.imageDataUrl, 'source.png')
    const uploadResult = await uploadAttachments([sourceFile])
    if (uploadResult.attachments.length === 0) {
      const message = uploadResult.errors[0]?.message ?? 'Could not prepare the image for editing.'
      throw new AiEditFailedError(message, uploadResult.errors[0]?.code ?? null)
    }
    const sourceAttachmentId = uploadResult.attachments[0].attachment_id

    try {
      const response = await aiEditAttachmentImage(sourceAttachmentId, {
        operation: request.operation,
        prompt: request.instruction,
        mask_data_url: request.maskDataUrl,
      })

      if (response.status !== 'completed' || !response.image_data_url || !response.attachment_id) {
        if (response.error_code === 'not_configured') throw new AiEditNotConfiguredError()
        throw new AiEditFailedError(
          response.error_message ?? 'The edit could not be completed.',
          response.error_code,
        )
      }

      return {
        imageDataUrl: response.image_data_url,
        attachmentId: response.attachment_id,
        provider: response.provider,
        model: response.model,
        warnings: response.warnings,
      }
    } catch (error) {
      if (error instanceof AiEditNotConfiguredError || error instanceof AiEditFailedError) throw error
      if (error instanceof ApiError) throw new AiEditFailedError(error.message, null)
      throw error
    } finally {
      // Best-effort cleanup of the scratch source upload -- every Generate
      // click re-snapshots the canvas fresh (see this module's own
      // docstring), so there is never a reason to keep this transient
      // attachment around; a failure here must never surface as if the
      // edit itself failed.
      void deleteAttachment(sourceAttachmentId).catch(() => {})
    }
  }
}
