import { act, renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ToastProvider } from '@/components/ui/toast'
import * as api from '@/lib/api'
import { MAX_CHAT_ATTACHMENTS, useChatAttachments } from './useChatAttachments'

function makeTextFile(name = 'notes.txt'): File {
  return new File(['hello world'], name, { type: 'text/plain' })
}

function makeImageFile(name = 'photo.png'): File {
  const PNG_SIGNATURE = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])
  return new File([PNG_SIGNATURE], name, { type: 'image/png' })
}

function wrapper({ children }: { children: ReactNode }) {
  return <ToastProvider>{children}</ToastProvider>
}

describe('useChatAttachments', () => {
  beforeEach(() => {
    vi.stubGlobal('URL', { ...URL, createObjectURL: vi.fn(() => 'blob:mock'), revokeObjectURL: vi.fn() })
  })

  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('uploads a document and marks it ready once the server responds', async () => {
    vi.spyOn(api, 'uploadAttachments').mockResolvedValue({
      attachments: [
        {
          attachment_id: 'att_1',
          filename: 'notes.txt',
          media_type: 'text/plain',
          size_bytes: 11,
          processing_status: 'succeeded',
          processing_error: null,
        },
      ],
      errors: [],
    })

    const { result } = renderHook(() => useChatAttachments(), { wrapper })
    act(() => {
      result.current.addFiles([makeTextFile()])
    })

    expect(result.current.attachments[0].status).toBe('uploading')
    expect(result.current.isUploading).toBe(true)

    await waitFor(() => expect(result.current.attachments[0].status).toBe('ready'))
    expect(result.current.attachments[0].attachmentId).toBe('att_1')
    expect(result.current.readyAttachmentIds).toEqual(['att_1'])
    expect(result.current.isUploading).toBe(false)
  })

  it('classifies an image file as kind "image" with a preview URL', async () => {
    vi.spyOn(api, 'uploadAttachments').mockResolvedValue({
      attachments: [
        {
          attachment_id: 'att_img',
          filename: 'photo.png',
          media_type: 'image/png',
          size_bytes: 8,
          processing_status: 'succeeded',
          processing_error: null,
        },
      ],
      errors: [],
    })

    const { result } = renderHook(() => useChatAttachments(), { wrapper })
    act(() => {
      result.current.addFiles([makeImageFile()])
    })

    expect(result.current.attachments[0].kind).toBe('image')
    expect(result.current.attachments[0].previewUrl).toBe('blob:mock')
  })

  it('marks an attachment as error when server-side processing fails', async () => {
    vi.spyOn(api, 'uploadAttachments').mockResolvedValue({
      attachments: [
        {
          attachment_id: 'att_bad',
          filename: 'bad.json',
          media_type: 'application/json',
          size_bytes: 5,
          processing_status: 'failed',
          processing_error: 'Invalid JSON.',
        },
      ],
      errors: [],
    })

    const { result } = renderHook(() => useChatAttachments(), { wrapper })
    act(() => {
      result.current.addFiles([makeTextFile('bad.json')])
    })

    await waitFor(() => expect(result.current.attachments[0].status).toBe('error'))
    expect(result.current.attachments[0].error).toBe('Invalid JSON.')
    expect(result.current.readyAttachmentIds).toEqual([])
  })

  it('marks an attachment as error when the upload is rejected by validation', async () => {
    vi.spyOn(api, 'uploadAttachments').mockResolvedValue({
      attachments: [],
      errors: [
        {
          code: 'UNSUPPORTED_FILE_TYPE',
          attachment_id: null,
          filename: 'virus.exe',
          message: "Files of type '.exe' aren't supported.",
        },
      ],
    })

    const { result } = renderHook(() => useChatAttachments(), { wrapper })
    act(() => {
      result.current.addFiles([new File(['x'], 'virus.exe', { type: 'application/octet-stream' })])
    })

    await waitFor(() => expect(result.current.attachments[0].status).toBe('error'))
    expect(result.current.attachments[0].error).toContain("aren't supported")
  })

  it('marks an attachment as error when the network request itself fails', async () => {
    vi.spyOn(api, 'uploadAttachments').mockRejectedValue(new Error('network down'))

    const { result } = renderHook(() => useChatAttachments(), { wrapper })
    act(() => {
      result.current.addFiles([makeTextFile()])
    })

    await waitFor(() => expect(result.current.attachments[0].status).toBe('error'))
  })

  it('rejects an empty file client-side without calling the API', () => {
    const uploadSpy = vi.spyOn(api, 'uploadAttachments')
    const { result } = renderHook(() => useChatAttachments(), { wrapper })
    act(() => {
      result.current.addFiles([new File([], 'empty.txt', { type: 'text/plain' })])
    })
    expect(result.current.attachments).toHaveLength(0)
    expect(uploadSpy).not.toHaveBeenCalled()
  })

  it('caps the number of attachments at MAX_CHAT_ATTACHMENTS', () => {
    vi.spyOn(api, 'uploadAttachments').mockResolvedValue({ attachments: [], errors: [] })
    const { result } = renderHook(() => useChatAttachments(), { wrapper })
    const files = Array.from({ length: MAX_CHAT_ATTACHMENTS + 2 }, (_, i) => makeTextFile(`f${i}.txt`))
    act(() => {
      result.current.addFiles(files)
    })
    expect(result.current.attachments).toHaveLength(MAX_CHAT_ATTACHMENTS)
    expect(result.current.canAddMore).toBe(false)
  })

  it('removeAttachment revokes the preview URL and deletes it server-side', async () => {
    vi.spyOn(api, 'uploadAttachments').mockResolvedValue({
      attachments: [
        {
          attachment_id: 'att_1',
          filename: 'photo.png',
          media_type: 'image/png',
          size_bytes: 8,
          processing_status: 'succeeded',
          processing_error: null,
        },
      ],
      errors: [],
    })
    const deleteSpy = vi.spyOn(api, 'deleteAttachment').mockResolvedValue(undefined)

    const { result } = renderHook(() => useChatAttachments(), { wrapper })
    act(() => {
      result.current.addFiles([makeImageFile()])
    })
    await waitFor(() => expect(result.current.attachments[0].status).toBe('ready'))

    act(() => {
      result.current.removeAttachment(result.current.attachments[0].id)
    })

    expect(result.current.attachments).toHaveLength(0)
    expect(deleteSpy).toHaveBeenCalledWith('att_1')
  })

  it('clearAll empties the composer and revokes every preview URL, without deleting anything server-side', async () => {
    vi.spyOn(api, 'uploadAttachments').mockResolvedValue({
      attachments: [
        {
          attachment_id: 'att_1',
          filename: 'photo.png',
          media_type: 'image/png',
          size_bytes: 8,
          processing_status: 'succeeded',
          processing_error: null,
        },
      ],
      errors: [],
    })
    const deleteSpy = vi.spyOn(api, 'deleteAttachment')
    const revokeSpy = vi.fn()
    vi.stubGlobal('URL', { ...URL, createObjectURL: vi.fn(() => 'blob:mock'), revokeObjectURL: revokeSpy })

    const { result } = renderHook(() => useChatAttachments(), { wrapper })
    act(() => {
      result.current.addFiles([makeImageFile()])
    })
    await waitFor(() => expect(result.current.attachments[0].status).toBe('ready'))

    act(() => {
      result.current.clearAll()
    })

    expect(result.current.attachments).toHaveLength(0)
    expect(revokeSpy).toHaveBeenCalledWith('blob:mock')
    // clearAll is for the already-sent case (see ChatInput.tsx's submit()) --
    // the attachment itself stays resolvable server-side, so a follow-up
    // question could still reference it by id if the caller kept it.
    expect(deleteSpy).not.toHaveBeenCalled()
  })

  it('addProcessedResult adds a resize/remove-text result as an already-ready chip, without re-uploading', async () => {
    const uploadSpy = vi.spyOn(api, 'uploadAttachments')
    // Stubs `fetch` directly (rather than relying on real data-URL fetch
    // support) since this file's own `beforeEach` above already stubs the
    // global `URL` constructor as a plain object for createObjectURL/
    // revokeObjectURL mocking -- which breaks `new URL()`, something
    // fetch()'s real implementation needs internally to parse a data: URL.
    const fakeBlob = new Blob(['fake-png-bytes'], { type: 'image/png' })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ blob: () => Promise.resolve(fakeBlob) }))
    const { result } = renderHook(() => useChatAttachments(), { wrapper })

    await act(async () => {
      await result.current.addProcessedResult(
        {
          attachment_id: 'att_resized',
          source_attachment_id: 'att_original',
          operation: 'resize',
          image_data_url: 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=',
          media_type: 'image/png',
          width: 100,
          height: 100,
          original_width: 200,
          original_height: 200,
          size_bytes: 68,
          warnings: [],
        },
        'photo_resized.png',
      )
    })

    expect(result.current.attachments).toHaveLength(1)
    const [item] = result.current.attachments
    expect(item.status).toBe('ready')
    expect(item.attachmentId).toBe('att_resized')
    expect(item.file.name).toBe('photo_resized.png')
    expect(item.kind).toBe('image')
    // The server already stored/processed this via register_derived_image
    // -- adding it to the composer must never trigger a second upload.
    expect(uploadSpy).not.toHaveBeenCalled()
  })
})
