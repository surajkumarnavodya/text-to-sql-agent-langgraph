/** The seam between the image editor UI and whatever actually performs an
 * edit. Two implementations exist today, with very different honesty
 * properties -- see docs/image-editing-architecture.md for the full
 * writeup:
 *
 * - `LocalCanvasAdapter`: does real work, entirely in the browser (crop,
 *   rotate, flip, draw, annotate, ...) -- exported via
 *   `stage.toDataURL()` in ImageEditor.tsx. No network call, no AI
 *   involved, and the UI never claims otherwise.
 * - `AiGuidedEditAdapter`: a deliberate, clearly-labeled STUB. There is no
 *   backend endpoint today that accepts an image + a mask + a natural-
 *   language instruction and returns an AI-edited result (confirmed: no
 *   such route exists in `api/`, no such field exists on `AskRequest`).
 *   Calling this adapter always rejects with `AiEditNotConfiguredError` --
 *   it exists so the UI can show the *affordance* (a prompt field, preset
 *   buttons) without lying about what happens when you use it. Wiring
 *   this up for real means building a backend endpoint shaped like
 *   `POST /media/edit` (mirroring `media_gen`'s existing generation
 *   pattern: human-approval gate, cost ceiling, SSRF-hardened storage) --
 *   seeing this exception in production is the signal that work hasn't
 *   been done, not a bug in this adapter.
 */

export interface AiGuidedEditRequest {
  /** The current edited image, as a data URL (image/png). */
  imageDataUrl: string
  /** An optional mask, also a data URL, in the same pixel dimensions as
   * `imageDataUrl` -- painted regions mark what the instruction applies
   * to. `null` means "the whole image." */
  maskDataUrl: string | null
  /** e.g. "Remove the selected object." / "Replace the sky with a sunset." */
  instruction: string
}

export interface AiGuidedEditResult {
  imageDataUrl: string
}

export class AiEditNotConfiguredError extends Error {
  constructor() {
    super(
      'AI-guided image editing requires a backend endpoint that does not exist yet in this deployment. ' +
        'See docs/image-editing-architecture.md for the endpoint contract this would need.',
    )
    this.name = 'AiEditNotConfiguredError'
  }
}

export interface ImageEditAdapter {
  editWithInstruction(request: AiGuidedEditRequest): Promise<AiGuidedEditResult>
}

/** The only adapter actually wired into the UI today. Always rejects --
 * see this module's own docstring for why that's the honest behavior,
 * not a bug. */
export class AiGuidedEditAdapter implements ImageEditAdapter {
  async editWithInstruction(_request: AiGuidedEditRequest): Promise<AiGuidedEditResult> {
    throw new AiEditNotConfiguredError()
  }
}
