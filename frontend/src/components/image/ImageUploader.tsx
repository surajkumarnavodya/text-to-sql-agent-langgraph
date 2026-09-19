import { ImagePlus } from 'lucide-react'
import { useRef, type ChangeEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'

/** The file-picker half of image attachment -- a plain icon button + a
 * hidden `<input type="file">`. Drag-and-drop and clipboard paste are
 * wired directly onto ChatInput's own container instead of duplicated
 * here (they need to work anywhere in the composer, not just on this one
 * button), calling the same `onFilesSelected` callback this component
 * uses -- see ChatInput.tsx's onDrop/onPaste handlers. */
export function ImageUploader({
  onFilesSelected,
  disabled,
}: {
  onFilesSelected: (files: FileList) => void
  disabled?: boolean
}) {
  const { t } = useTranslation()
  const inputRef = useRef<HTMLInputElement>(null)

  const handleChange = (event: ChangeEvent<HTMLInputElement>) => {
    if (event.target.files && event.target.files.length > 0) {
      onFilesSelected(event.target.files)
    }
    // Reset so selecting the exact same file again still fires onChange.
    event.target.value = ''
  }

  return (
    <>
      <input
        ref={inputRef}
        type="file"
        accept="image/png,image/jpeg,image/webp,image/gif"
        multiple
        onChange={handleChange}
        className="hidden"
        aria-hidden="true"
        tabIndex={-1}
      />
      <Button
        variant="secondary"
        size="icon"
        onClick={() => inputRef.current?.click()}
        disabled={disabled}
        aria-label={t('image.attach')}
        title={t('image.attach')}
        className="rounded-xl"
      >
        <ImagePlus className="h-4 w-4" />
      </Button>
    </>
  )
}
