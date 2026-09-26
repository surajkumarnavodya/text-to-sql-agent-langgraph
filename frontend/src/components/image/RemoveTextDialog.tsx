import { AlertTriangle, Download, Loader2, Trash2 } from 'lucide-react'
import { useEffect, useRef, useState, type MouseEvent as ReactMouseEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogFooter, DialogTitle } from '@/components/ui/dialog'
import { ApiError, detectAttachmentTextRegions, removeAttachmentText } from '@/lib/api'
import type { ImageEditResultResponse, ImageRegionIn, TextLineRegionOut } from '@/lib/types'

interface DraftRect {
  left: number
  top: number
  width: number
  height: number
}

/** Converts a mouse event's position, relative to `img`'s own rendered
 * bounding box, into source-image pixel coordinates -- the coordinate
 * space every region sent to `POST /attachments/{id}/remove-text` must be
 * in, regardless of how the image is currently scaled on screen. */
function toNaturalPoint(
  event: ReactMouseEvent<HTMLDivElement>,
  img: HTMLImageElement,
): { x: number; y: number } {
  const rect = img.getBoundingClientRect()
  const fractionX = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width))
  const fractionY = Math.min(1, Math.max(0, (event.clientY - rect.top) / rect.height))
  return { x: Math.round(fractionX * img.naturalWidth), y: Math.round(fractionY * img.naturalHeight) }
}

/** "Remove text" -- capability (D) in CLAUDE.md's "four capabilities"
 * split: real pixel editing via classical (OpenCV) inpainting, never a
 * solid rectangle or a CSS overlay. Regions are either OCR-detected (this
 * dialog auto-detects on open, via `GET /attachments/{id}/detect-text-regions`,
 * and the user confirms/deselects which to remove) or manually drawn
 * directly on the preview when nothing was auto-detected (OCR unavailable,
 * or the image has no recognizable text) -- either path is a real,
 * supported way to select regions, per this feature's own "automatic
 * detection with confirmation, OR manual selection" requirement. */
export function RemoveTextDialog({
  open,
  onOpenChange,
  attachmentId,
  filename,
  previewUrl,
  maxRegions,
  onEdited,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  attachmentId: string
  filename: string
  previewUrl: string | null
  maxRegions: number
  onEdited: (result: ImageEditResultResponse) => void
}) {
  const { t } = useTranslation()
  const imgRef = useRef<HTMLImageElement>(null)
  const [phase, setPhase] = useState<'detecting' | 'ready' | 'applying' | 'succeeded' | 'error'>('detecting')
  const [detectedRegions, setDetectedRegions] = useState<TextLineRegionOut[]>([])
  const [excludedRegionIds, setExcludedRegionIds] = useState<Set<number>>(new Set())
  const [manualRegions, setManualRegions] = useState<ImageRegionIn[]>([])
  const [draft, setDraft] = useState<DraftRect | null>(null)
  const dragStart = useRef<{ x: number; y: number } | null>(null)
  const [errorMessage, setErrorMessage] = useState<string | null>(null)
  const [result, setResult] = useState<ImageEditResultResponse | null>(null)
  // Set via the <img>'s own onLoad, not read from imgRef.current during
  // render -- reading a ref's current value at render time doesn't trigger
  // a re-render when it changes, which would leave the overlay boxes
  // computed from a stale (or still-null) natural size on first paint.
  const [naturalSize, setNaturalSize] = useState<{ width: number; height: number } | null>(null)

  useEffect(() => {
    if (!open) return
    setPhase('detecting')
    setDetectedRegions([])
    setExcludedRegionIds(new Set())
    setManualRegions([])
    setDraft(null)
    setErrorMessage(null)
    setResult(null)
    setNaturalSize(null)

    let cancelled = false
    void detectAttachmentTextRegions(attachmentId)
      .then((response) => {
        if (cancelled) return
        setDetectedRegions(response.regions)
        setPhase('ready')
      })
      .catch(() => {
        // Fails open -- an unreachable OCR engine is not a reason "Remove
        // text" can't work, it just means manual selection is the only
        // path available for this image.
        if (!cancelled) setPhase('ready')
      })
    return () => {
      cancelled = true
    }
  }, [open, attachmentId])

  const activeRegions: ImageRegionIn[] = [
    ...detectedRegions
      .filter((region) => !excludedRegionIds.has(region.region_id))
      .map((region) => ({ left: region.left, top: region.top, width: region.width, height: region.height })),
    ...manualRegions,
  ]
  const atCapacity = activeRegions.length >= maxRegions

  const toggleDetectedRegion = (regionId: number) => {
    setExcludedRegionIds((current) => {
      const next = new Set(current)
      if (next.has(regionId)) next.delete(regionId)
      else next.add(regionId)
      return next
    })
  }

  const handleMouseDown = (event: ReactMouseEvent<HTMLDivElement>) => {
    if (!imgRef.current || atCapacity) return
    const point = toNaturalPoint(event, imgRef.current)
    dragStart.current = { x: point.x, y: point.y }
    setDraft({ left: point.x, top: point.y, width: 0, height: 0 })
  }

  const handleMouseMove = (event: ReactMouseEvent<HTMLDivElement>) => {
    if (!dragStart.current || !imgRef.current) return
    const point = toNaturalPoint(event, imgRef.current)
    const left = Math.min(dragStart.current.x, point.x)
    const top = Math.min(dragStart.current.y, point.y)
    setDraft({ left, top, width: Math.abs(point.x - dragStart.current.x), height: Math.abs(point.y - dragStart.current.y) })
  }

  const handleMouseUp = () => {
    if (draft && draft.width > 4 && draft.height > 4) {
      setManualRegions((current) => [...current, draft])
    }
    dragStart.current = null
    setDraft(null)
  }

  const removeManualRegion = (index: number) => {
    setManualRegions((current) => current.filter((_, i) => i !== index))
  }

  const handleApply = async () => {
    if (activeRegions.length === 0) {
      setErrorMessage(t('attachments.removeText.errorNoRegions'))
      setPhase('error')
      return
    }
    setPhase('applying')
    setErrorMessage(null)
    try {
      const response = await removeAttachmentText(attachmentId, activeRegions)
      setResult(response)
      setPhase('succeeded')
    } catch (error) {
      setErrorMessage(error instanceof ApiError ? error.message : t('attachments.removeText.errorGeneric'))
      setPhase('error')
    }
  }

  const toPercent = (region: { left: number; top: number; width: number; height: number }) => ({
    left: `${(region.left / (naturalSize?.width || 1)) * 100}%`,
    top: `${(region.top / (naturalSize?.height || 1)) * 100}%`,
    width: `${(region.width / (naturalSize?.width || 1)) * 100}%`,
    height: `${(region.height / (naturalSize?.height || 1)) * 100}%`,
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogTitle>{t('attachments.removeText.title')}</DialogTitle>
        <p className="mb-3 truncate text-xs text-[var(--muted-foreground)]" title={filename}>
          {filename}
        </p>

        {phase === 'succeeded' && result ? (
          <div className="flex flex-col gap-3">
            <div className="grid grid-cols-2 gap-2">
              <div className="flex flex-col gap-1">
                <span className="text-center text-xs font-medium text-[var(--muted-foreground)]">
                  {t('attachments.removeText.beforeLabel')}
                </span>
                {previewUrl && (
                  <img src={previewUrl} alt="" className="rounded-md border border-[var(--border)] object-contain" />
                )}
              </div>
              <div className="flex flex-col gap-1">
                <span className="text-center text-xs font-medium text-[var(--muted-foreground)]">
                  {t('attachments.removeText.afterLabel')}
                </span>
                <img
                  src={result.image_data_url}
                  alt=""
                  className="rounded-md border border-[var(--border)] object-contain"
                />
              </div>
            </div>
            {result.warnings.map((warning) => (
              <div
                key={warning}
                className="flex items-start gap-2 rounded-md border border-[var(--warning,#d97706)]/30 bg-[var(--warning,#d97706)]/10 p-2.5 text-xs text-[var(--warning,#d97706)]"
              >
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                <span>{warning}</span>
              </div>
            ))}
            <DialogFooter className="mt-0 justify-center">
              <Button variant="secondary" onClick={() => downloadResult(result, filename)}>
                <Download className="h-4 w-4" />
                {t('attachments.removeText.downloadResult')}
              </Button>
              <Button
                onClick={() => {
                  onEdited(result)
                  onOpenChange(false)
                }}
              >
                {t('attachments.removeText.attachResult')}
              </Button>
            </DialogFooter>
          </div>
        ) : (
          <div className="flex flex-col gap-3">
            {phase === 'detecting' && (
              <div className="flex items-center justify-center gap-2 py-6 text-sm text-[var(--muted-foreground)]">
                <Loader2 className="h-4 w-4 animate-spin" />
                {t('attachments.removeText.detecting')}
              </div>
            )}

            {previewUrl && phase !== 'detecting' && (
              <>
                <p className="text-xs text-[var(--muted-foreground)]">
                  {detectedRegions.length > 0
                    ? t('attachments.removeText.detectedRegionsHint')
                    : t('attachments.removeText.noRegionsDetected')}
                </p>
                <div
                  className="relative w-full cursor-crosshair select-none"
                  onMouseDown={handleMouseDown}
                  onMouseMove={handleMouseMove}
                  onMouseUp={handleMouseUp}
                  onMouseLeave={() => {
                    dragStart.current = null
                    setDraft(null)
                  }}
                >
                  <img
                    ref={imgRef}
                    src={previewUrl}
                    alt=""
                    draggable={false}
                    onLoad={(event) => {
                      const target = event.currentTarget
                      setNaturalSize({ width: target.naturalWidth, height: target.naturalHeight })
                    }}
                    className="w-full rounded-md border border-[var(--border)] object-contain"
                  />
                  {detectedRegions.map((region) => {
                    const excluded = excludedRegionIds.has(region.region_id)
                    return (
                      <button
                        key={region.region_id}
                        type="button"
                        onClick={() => toggleDetectedRegion(region.region_id)}
                        title={region.text}
                        style={toPercent(region)}
                        className={`absolute rounded-sm border-2 transition-colors ${
                          excluded
                            ? 'border-[var(--muted-foreground)]/40 bg-transparent'
                            : 'border-[var(--accent)] bg-[var(--accent)]/20'
                        }`}
                      />
                    )
                  })}
                  {manualRegions.map((region, index) => (
                    <div
                      key={`manual-${index}`}
                      style={toPercent(region)}
                      className="absolute rounded-sm border-2 border-[var(--danger)] bg-[var(--danger)]/15"
                    />
                  ))}
                  {draft && (
                    <div
                      style={toPercent(draft)}
                      className="pointer-events-none absolute rounded-sm border-2 border-dashed border-[var(--accent)]"
                    />
                  )}
                </div>
                <p className="text-[11px] text-[var(--muted-foreground)]">
                  {t('attachments.removeText.drawHint')}
                </p>
              </>
            )}

            {manualRegions.length > 0 && (
              <div className="flex flex-col gap-1">
                <span className="text-xs font-medium text-[var(--muted-foreground)]">
                  {t('attachments.removeText.regionsLabel')} ({activeRegions.length})
                </span>
                <div className="flex flex-wrap gap-1.5">
                  {manualRegions.map((_, index) => (
                    <Button key={index} size="sm" variant="secondary" onClick={() => removeManualRegion(index)}>
                      <Trash2 className="h-3 w-3" />
                      {t('attachments.removeText.removeRegion')} {index + 1}
                    </Button>
                  ))}
                </div>
              </div>
            )}

            {atCapacity && (
              <p className="text-[11px] text-[var(--muted-foreground)]">
                {t('attachments.removeText.tooManyRegions', { max: maxRegions })}
              </p>
            )}

            {phase === 'error' && errorMessage && (
              <div className="flex items-start gap-2 rounded-md border border-[var(--danger)]/30 bg-[var(--danger)]/10 p-2.5 text-xs text-[var(--danger)]">
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                <span>{errorMessage}</span>
              </div>
            )}

            <DialogFooter className="mt-0">
              <Button
                onClick={() => void handleApply()}
                disabled={phase === 'applying' || phase === 'detecting' || activeRegions.length === 0}
              >
                {phase === 'applying' ? (
                  <>
                    <Loader2 className="h-4 w-4 animate-spin" />
                    {t('attachments.removeText.applying')}
                  </>
                ) : (
                  t('attachments.removeText.apply')
                )}
              </Button>
            </DialogFooter>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}

function downloadResult(result: ImageEditResultResponse, filename: string): void {
  const link = document.createElement('a')
  link.href = result.image_data_url
  link.download = filename
  link.click()
}
