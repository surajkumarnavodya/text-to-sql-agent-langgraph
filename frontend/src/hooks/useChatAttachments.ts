import { useCallback, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useToast } from '@/components/ui/toast'
import { deleteAttachment as apiDeleteAttachment, uploadAttachments } from '@/lib/api'
import { formatFileSize } from '@/lib/imageValidation'
import type { ImageEditResultResponse } from '@/lib/types'

export const MAX_CHAT_ATTACHMENTS = 5
export const MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

export interface ChatAttachment {
  /** Local, client-only id -- React key + removal target. Never sent to
   * the backend (that's `attachmentId`, once upload succeeds). */
  id: string
  file: File
  kind: 'image' | 'document'
  /** Object URL of the original file, for an image's thumbnail preview.
   * Revoked on removal. Null for a document (no visual preview). */
  previewUrl: string | null
  /** Set once the user saves changes in ImageEditor -- a data URL. Images only. */
  editedDataUrl: string | null
  status: 'uploading' | 'ready' | 'error'
  /** The server-assigned id (attachments.models.Attachment.attachment_id)
   * once upload succeeds -- this, not the file itself, is what gets sent
   * as AskRequest.attachment_ids. Null while uploading/on error. */
  attachmentId: string | null
  /** Set only when status is 'error' (upload or server-side processing
   * failed) -- shown inline on the chip, never silently dropped. */
  error: string | null
}

const IMAGE_TYPE_PREFIX = 'image/'

/** Owns every file attached to the composer -- both images and documents
 * (PDF/DOCX/XLSX/PPTX/TXT/MD/CSV/JSON) -- uploading each to the backend
 * immediately on selection (unlike the old image-only, local-only
 * useImageAttachments, "attaching" a file here means it actually reaches
 * the model: see attachments/pipeline.py). Each file uploads independently
 * (one request per file, not batched) so one chip's status never blocks or
 * gets confused with another's. */
export function useChatAttachments() {
  const { t } = useTranslation()
  const { toast } = useToast()
  const [attachments, setAttachments] = useState<ChatAttachment[]>([])
  const nextId = useRef(0)

  const updateItem = useCallback((id: string, patch: Partial<ChatAttachment>) => {
    setAttachments((current) => current.map((item) => (item.id === id ? { ...item, ...patch } : item)))
  }, [])

  const uploadOne = useCallback(
    async (id: string, file: File) => {
      try {
        const response = await uploadAttachments([file])
        const uploaded = response.attachments[0]
        const failure = response.errors[0]
        if (uploaded) {
          if (uploaded.processing_status === 'failed') {
            updateItem(id, {
              status: 'error',
              attachmentId: uploaded.attachment_id,
              error: uploaded.processing_error ?? t('attachments.processingFailed'),
            })
          } else {
            updateItem(id, { status: 'ready', attachmentId: uploaded.attachment_id, error: null })
          }
        } else {
          updateItem(id, { status: 'error', error: failure?.message ?? t('attachments.uploadFailed') })
        }
      } catch {
        updateItem(id, { status: 'error', error: t('attachments.uploadFailed') })
      }
    },
    [t, updateItem],
  )

  const addFiles = useCallback(
    (files: FileList | File[]) => {
      const list = Array.from(files)
      let count = attachments.length
      for (const file of list) {
        if (count >= MAX_CHAT_ATTACHMENTS) {
          toast({ title: t('attachments.errorTooMany', { max: MAX_CHAT_ATTACHMENTS }), variant: 'error' })
          break
        }
        if (file.size === 0) {
          toast({ title: file.name, description: t('attachments.errorEmpty'), variant: 'error' })
          continue
        }
        if (file.size > MAX_ATTACHMENT_BYTES) {
          toast({ title: file.name, description: t('attachments.errorTooLarge'), variant: 'error' })
          continue
        }
        const isImage = file.type.startsWith(IMAGE_TYPE_PREFIX)
        const id = `attachment-${nextId.current++}`
        count += 1
        setAttachments((current) => [
          ...current,
          {
            id,
            file,
            kind: isImage ? 'image' : 'document',
            previewUrl: isImage ? URL.createObjectURL(file) : null,
            editedDataUrl: null,
            status: 'uploading',
            attachmentId: null,
            error: null,
          },
        ])
        void uploadOne(id, file)
      }
    },
    [attachments.length, t, toast, uploadOne],
  )

  const removeAttachment = useCallback((id: string) => {
    setAttachments((current) => {
      const target = current.find((item) => item.id === id)
      if (target?.previewUrl) URL.revokeObjectURL(target.previewUrl)
      if (target?.attachmentId) void apiDeleteAttachment(target.attachmentId)
      return current.filter((item) => item.id !== id)
    })
  }, [])

  /** Re-uploads an edited image's bytes as a fresh attachment (a new
   * attachment_id) and swaps it in, so the model sees the edited version --
   * without this, the backend would still only ever have the original,
   * pre-edit bytes tied to the old attachment_id. The old, now-orphaned
   * attachment_id is deleted server-side in the background. `editedDataUrl`
   * (Konva's `toDataURL()` output, see ImageEditor.tsx) is converted to a
   * File via a data-URL fetch -- the standard, dependency-free way to turn
   * one back into a Blob in a browser. */
  const setEditedImage = useCallback(
    (id: string, editedDataUrl: string) => {
      const target = attachments.find((item) => item.id === id)
      if (!target) return
      setAttachments((current) =>
        current.map((item) =>
          item.id === id
            ? { ...item, editedDataUrl, status: 'uploading' as const, error: null }
            : item,
        ),
      )
      void fetch(editedDataUrl)
        .then((res) => res.blob())
        .then((blob) => new File([blob], target.file.name, { type: blob.type }))
        .then((editedFile) => uploadOne(id, editedFile))
        .then(() => {
          if (target.attachmentId) void apiDeleteAttachment(target.attachmentId)
        })
    },
    [attachments, uploadOne],
  )

  /** Adds a resize/remove-text result (`ImageEditResultResponse`) as a new
   * composer chip -- the server already stored and processed it
   * (`attachments.pipeline.register_derived_image`), so this never
   * re-uploads anything, unlike `addFiles`. This is what makes "Attach
   * resized image"/"Attach edited image" actually send the *edited* bytes
   * on the next question, not the original: the new `attachmentId` is what
   * `ChatInput.submit()` reads off this item, same as any other ready
   * attachment. The data-URL-to-File conversion (matching `setEditedImage`'s
   * own pattern below) is purely for accurate local display (filename/size
   * in the chip) -- the bytes it carries are never re-sent anywhere. */
  const addProcessedResult = useCallback(async (result: ImageEditResultResponse, filename: string) => {
    const blob = await fetch(result.image_data_url).then((res) => res.blob())
    const file = new File([blob], filename, { type: result.media_type })
    const id = `attachment-${nextId.current++}`
    setAttachments((current) => [
      ...current,
      {
        id,
        file,
        kind: 'image' as const,
        previewUrl: result.image_data_url,
        editedDataUrl: null,
        status: 'ready' as const,
        attachmentId: result.attachment_id,
        error: null,
      },
    ])
  }, [])

  const clearAll = useCallback(() => {
    setAttachments((current) => {
      for (const item of current) {
        if (item.previewUrl) URL.revokeObjectURL(item.previewUrl)
      }
      return []
    })
  }, [])

  const isUploading = attachments.some((item) => item.status === 'uploading')
  const readyAttachmentIds = attachments
    .filter((item) => item.status === 'ready' && item.attachmentId)
    .map((item) => item.attachmentId as string)

  return {
    attachments,
    addFiles,
    removeAttachment,
    setEditedImage,
    addProcessedResult,
    clearAll,
    isUploading,
    readyAttachmentIds,
    canAddMore: attachments.length < MAX_CHAT_ATTACHMENTS,
  }
}

export { formatFileSize }
