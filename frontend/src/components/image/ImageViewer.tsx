import { Minus, Plus, RotateCcw } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog'

const MIN_ZOOM = 0.5
const MAX_ZOOM = 4

/** A lightbox for one attached image -- zoom (buttons + scroll-wheel),
 * pan via native scroll (the image container is `overflow-auto`, so once
 * zoomed past 1x the browser's own scrollbars/trackpad panning apply --
 * not a custom drag-to-pan implementation), close button, and
 * Escape-to-close (both from the shared Dialog primitive's own Radix
 * behavior). No prev/next
 * navigation: this views one attachment at a time, opened from its own
 * AttachmentChip, not a gallery of everything in the composer -- multiple
 * open images in a chat message is not yet a real scenario since nothing
 * is actually sent to the backend today (see docs/image-editing-
 * architecture.md), so there is no rendered-in-chat gallery to navigate
 * between. */
export function ImageViewer({
  open,
  onOpenChange,
  imageSrc,
  altText,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  imageSrc: string
  altText: string
}) {
  const { t } = useTranslation()
  const [zoom, setZoom] = useState(1)

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) setZoom(1)
        onOpenChange(next)
      }}
    >
      <DialogContent className="max-w-4xl">
        <DialogTitle className="sr-only">{t('image.viewerTitle')}</DialogTitle>
        <div className="flex items-center justify-end gap-1 pb-2">
          <Button
            size="icon"
            variant="ghost"
            onClick={() => setZoom((z) => Math.max(MIN_ZOOM, z - 0.25))}
            aria-label={t('image.zoomOut')}
            title={t('image.zoomOut')}
          >
            <Minus className="h-4 w-4" />
          </Button>
          <Button
            size="icon"
            variant="ghost"
            onClick={() => setZoom((z) => Math.min(MAX_ZOOM, z + 0.25))}
            aria-label={t('image.zoomIn')}
            title={t('image.zoomIn')}
          >
            <Plus className="h-4 w-4" />
          </Button>
          <Button
            size="icon"
            variant="ghost"
            onClick={() => setZoom(1)}
            aria-label={t('image.resetEdits')}
            title={t('image.resetEdits')}
          >
            <RotateCcw className="h-4 w-4" />
          </Button>
        </div>
        <div
          className="flex max-h-[70vh] items-center justify-center overflow-auto rounded-md bg-[var(--muted)]"
          onWheel={(event) => {
            event.preventDefault()
            setZoom((z) => Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, z - event.deltaY * 0.001)))
          }}
        >
          <img
            src={imageSrc}
            alt={altText}
            style={{ transform: `scale(${zoom})`, transition: 'transform 150ms ease' }}
            className="max-h-[68vh] max-w-full object-contain"
          />
        </div>
      </DialogContent>
    </Dialog>
  )
}
