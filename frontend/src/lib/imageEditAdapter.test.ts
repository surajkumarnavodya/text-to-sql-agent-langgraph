import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AttachmentUploadResponse, AiImageEditResponse } from './types'

const uploadAttachments = vi.fn<(files: File[]) => Promise<AttachmentUploadResponse>>()
const aiEditAttachmentImage = vi.fn<(id: string, body: unknown) => Promise<AiImageEditResponse>>()
const deleteAttachment = vi.fn<(id: string) => Promise<void>>()

class FakeApiError extends Error {
  status: number
  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

vi.mock('./api', () => ({
  uploadAttachments: (files: File[]) => uploadAttachments(files),
  aiEditAttachmentImage: (id: string, body: unknown) => aiEditAttachmentImage(id, body),
  deleteAttachment: (id: string) => deleteAttachment(id),
  ApiError: FakeApiError,
}))

function successfulUpload(attachmentId = 'source-att-1'): AttachmentUploadResponse {
  return {
    attachments: [
      {
        attachment_id: attachmentId,
        filename: 'source.png',
        media_type: 'image/png',
        size_bytes: 128,
        processing_status: 'ready',
        processing_error: null,
      },
    ],
    errors: [],
  }
}

const SOURCE_DATA_URL = 'data:image/png;base64,AAAA'

describe('AiGuidedEditAdapter.editWithInstruction', () => {
  beforeEach(() => {
    uploadAttachments.mockReset()
    aiEditAttachmentImage.mockReset()
    deleteAttachment.mockReset().mockResolvedValue(undefined)
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  it('uploads the current canvas export, calls the ai-edit endpoint with it, and returns the result', async () => {
    const { AiGuidedEditAdapter } = await import('./imageEditAdapter')
    uploadAttachments.mockResolvedValue(successfulUpload('source-att-1'))
    aiEditAttachmentImage.mockResolvedValue({
      operation: 'remove_object',
      status: 'completed',
      source_attachment_id: 'source-att-1',
      mask_provided: true,
      attachment_id: 'derived-att-9',
      image_data_url: 'data:image/png;base64,RESULT',
      media_type: 'image/png',
      width: 100,
      height: 80,
      size_bytes: 900,
      provider: 'ima_studio',
      model: 'gpt-image-2',
      warnings: ['Mask conveyed as a translucent overlay, not pixel-exact.'],
      error_code: null,
      error_message: null,
    })

    const adapter = new AiGuidedEditAdapter()
    const result = await adapter.editWithInstruction({
      imageDataUrl: SOURCE_DATA_URL,
      maskDataUrl: 'data:image/png;base64,MASK',
      instruction: 'Remove the selected object.',
      operation: 'remove_object',
    })

    expect(result).toEqual({
      imageDataUrl: 'data:image/png;base64,RESULT',
      attachmentId: 'derived-att-9',
      provider: 'ima_studio',
      model: 'gpt-image-2',
      warnings: ['Mask conveyed as a translucent overlay, not pixel-exact.'],
    })

    // The *current edited canvas*, not a stale original, is what gets uploaded.
    expect(uploadAttachments).toHaveBeenCalledTimes(1)
    const uploadedFiles = uploadAttachments.mock.calls[0][0]
    expect(uploadedFiles).toHaveLength(1)
    expect(uploadedFiles[0].type).toBe('image/png')

    // The ai-edit call carries the mask and instruction through untouched.
    expect(aiEditAttachmentImage).toHaveBeenCalledWith('source-att-1', {
      operation: 'remove_object',
      prompt: 'Remove the selected object.',
      mask_data_url: 'data:image/png;base64,MASK',
    })

    // The transient source upload is always cleaned up, success or not.
    expect(deleteAttachment).toHaveBeenCalledWith('source-att-1')
  })

  it('passes maskDataUrl through as null when no mask was painted', async () => {
    const { AiGuidedEditAdapter } = await import('./imageEditAdapter')
    uploadAttachments.mockResolvedValue(successfulUpload())
    aiEditAttachmentImage.mockResolvedValue({
      operation: 'enhance',
      status: 'completed',
      source_attachment_id: 'source-att-1',
      mask_provided: false,
      attachment_id: 'derived-att-2',
      image_data_url: 'data:image/png;base64,RESULT',
      media_type: 'image/png',
      width: 100,
      height: 80,
      size_bytes: 900,
      provider: 'ima_studio',
      model: 'gpt-image-2',
      warnings: [],
      error_code: null,
      error_message: null,
    })

    const adapter = new AiGuidedEditAdapter()
    await adapter.editWithInstruction({
      imageDataUrl: SOURCE_DATA_URL,
      maskDataUrl: null,
      instruction: 'Enhance the selected region.',
      operation: 'enhance',
    })

    expect(aiEditAttachmentImage).toHaveBeenCalledWith(
      'source-att-1',
      expect.objectContaining({ mask_data_url: null }),
    )
  })

  it('throws AiEditNotConfiguredError when the backend reports not_configured, and still cleans up', async () => {
    const { AiGuidedEditAdapter, AiEditNotConfiguredError } = await import('./imageEditAdapter')
    uploadAttachments.mockResolvedValue(successfulUpload())
    aiEditAttachmentImage.mockResolvedValue({
      operation: 'remove_object',
      status: 'failed',
      source_attachment_id: 'source-att-1',
      mask_provided: false,
      attachment_id: null,
      image_data_url: null,
      media_type: null,
      width: null,
      height: null,
      size_bytes: null,
      provider: null,
      model: null,
      warnings: [],
      error_code: 'not_configured',
      error_message: 'AI-guided image editing is not configured on this server.',
    })

    const adapter = new AiGuidedEditAdapter()
    await expect(
      adapter.editWithInstruction({
        imageDataUrl: SOURCE_DATA_URL,
        maskDataUrl: null,
        instruction: 'Remove the selected object.',
        operation: 'remove_object',
      }),
    ).rejects.toBeInstanceOf(AiEditNotConfiguredError)

    expect(deleteAttachment).toHaveBeenCalledWith('source-att-1')
  })

  it('throws AiEditFailedError with the server error code/message for an ordinary failed edit', async () => {
    const { AiGuidedEditAdapter, AiEditFailedError } = await import('./imageEditAdapter')
    uploadAttachments.mockResolvedValue(successfulUpload())
    aiEditAttachmentImage.mockResolvedValue({
      operation: 'remove_object',
      status: 'failed',
      source_attachment_id: 'source-att-1',
      mask_provided: true,
      attachment_id: null,
      image_data_url: null,
      media_type: null,
      width: null,
      height: null,
      size_bytes: null,
      provider: null,
      model: null,
      warnings: [],
      error_code: 'rate_limited',
      error_message: 'Too many image edits requested. Please wait and try again.',
    })

    const adapter = new AiGuidedEditAdapter()
    const error = await adapter
      .editWithInstruction({
        imageDataUrl: SOURCE_DATA_URL,
        maskDataUrl: 'data:image/png;base64,MASK',
        instruction: 'Remove the selected object.',
        operation: 'remove_object',
      })
      .catch((e: unknown) => e)

    expect(error).toBeInstanceOf(AiEditFailedError)
    expect((error as InstanceType<typeof AiEditFailedError>).errorCode).toBe('rate_limited')
    expect((error as Error).message).toBe('Too many image edits requested. Please wait and try again.')
    expect(deleteAttachment).toHaveBeenCalledWith('source-att-1')
  })

  it('wraps a genuine transport/ApiError failure from the ai-edit call as AiEditFailedError and still cleans up', async () => {
    const { AiGuidedEditAdapter, AiEditFailedError } = await import('./imageEditAdapter')
    uploadAttachments.mockResolvedValue(successfulUpload())
    aiEditAttachmentImage.mockRejectedValue(new FakeApiError('Request failed with status 500.', 500))

    const adapter = new AiGuidedEditAdapter()
    const error = await adapter
      .editWithInstruction({
        imageDataUrl: SOURCE_DATA_URL,
        maskDataUrl: null,
        instruction: 'Remove the selected object.',
        operation: 'remove_object',
      })
      .catch((e: unknown) => e)

    expect(error).toBeInstanceOf(AiEditFailedError)
    expect((error as Error).message).toBe('Request failed with status 500.')
    expect(deleteAttachment).toHaveBeenCalledWith('source-att-1')
  })

  it('throws AiEditFailedError immediately when the upload itself fails, without ever calling ai-edit', async () => {
    const { AiGuidedEditAdapter, AiEditFailedError } = await import('./imageEditAdapter')
    uploadAttachments.mockResolvedValue({
      attachments: [],
      errors: [
        {
          code: 'file_too_large',
          attachment_id: null,
          filename: 'source.png',
          message: 'This image is too large to upload.',
        },
      ],
    })

    const adapter = new AiGuidedEditAdapter()
    const error = await adapter
      .editWithInstruction({
        imageDataUrl: SOURCE_DATA_URL,
        maskDataUrl: null,
        instruction: 'Remove the selected object.',
        operation: 'remove_object',
      })
      .catch((e: unknown) => e)

    expect(error).toBeInstanceOf(AiEditFailedError)
    expect((error as Error).message).toBe('This image is too large to upload.')
    expect(aiEditAttachmentImage).not.toHaveBeenCalled()
    // Nothing was ever uploaded successfully, so there is nothing to delete.
    expect(deleteAttachment).not.toHaveBeenCalled()
  })
})
