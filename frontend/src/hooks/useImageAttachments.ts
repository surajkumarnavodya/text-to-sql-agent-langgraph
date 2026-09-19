import { useCallback, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useToast } from '@/components/ui/toast'
import { MAX_ATTACHED_IMAGES, validateImageFile } from '@/lib/imageValidation'

export interface AttachedImage {
  id: string
  file: File
  /** Object URL of the original, unedited file -- revoked on removal. */
  originalUrl: string
  width: number
  height: number
  /** Set once the user saves changes in ImageEditor -- a data URL, not an
   * object URL, since it's generated in-memory by Konva's `toDataURL()`
   * rather than backed by a real File/Blob. `null` means "never edited,"
   * distinct from an edit that happens to look identical to the original. */
  editedDataUrl: string | null
}

/** Owns the list of images currently attached to the composer -- add
 * (with real validation, see lib/imageValidation.ts), remove, replace, and
 * record an edit. Capped at MAX_ATTACHED_IMAGES. Entirely local/in-memory:
 * there is no upload step, because no backend endpoint exists yet to
 * upload to (see docs/image-editing-architecture.md) -- "attaching" an
 * image today means "hold it in this browser tab's memory for local
 * preview/editing," nothing more. */
export function useImageAttachments() {
  const { t } = useTranslation()
  const { toast } = useToast()
  const [images, setImages] = useState<AttachedImage[]>([])
  const nextId = useRef(0)

  const addFiles = useCallback(
    async (files: FileList | File[]) => {
      const list = Array.from(files)
      // A local running count, not `images.length` from closure -- this
      // loop can add several files in sequence, each via its own
      // functional setImages call, so the closure's snapshot of `images`
      // never reflects a file added earlier in the same call.
      let count = images.length
      for (const file of list) {
        if (count >= MAX_ATTACHED_IMAGES) {
          toast({ title: t('image.errorTooMany', { max: MAX_ATTACHED_IMAGES }), variant: 'error' })
          break
        }
        const result = await validateImageFile(file)
        if (!result.ok) {
          toast({ title: file.name, description: t(result.errorKey ?? 'image.errorUnsupportedType'), variant: 'error' })
          continue
        }
        const id = `image-${nextId.current++}`
        const originalUrl = URL.createObjectURL(file)
        count += 1
        setImages((current) => [
          ...current,
          {
            id,
            file,
            originalUrl,
            width: result.width ?? 0,
            height: result.height ?? 0,
            editedDataUrl: null,
          },
        ])
      }
    },
    [images.length, t, toast],
  )

  const removeImage = useCallback((id: string) => {
    setImages((current) => {
      const target = current.find((img) => img.id === id)
      if (target) URL.revokeObjectURL(target.originalUrl)
      return current.filter((img) => img.id !== id)
    })
  }, [])

  const setEditedImage = useCallback((id: string, editedDataUrl: string) => {
    setImages((current) => current.map((img) => (img.id === id ? { ...img, editedDataUrl } : img)))
  }, [])

  const clearAll = useCallback(() => {
    setImages((current) => {
      for (const img of current) URL.revokeObjectURL(img.originalUrl)
      return []
    })
  }, [])

  return { images, addFiles, removeImage, setEditedImage, clearAll, canAddMore: images.length < MAX_ATTACHED_IMAGES }
}
