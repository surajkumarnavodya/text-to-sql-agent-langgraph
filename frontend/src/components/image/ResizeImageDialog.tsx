import { AlertTriangle, Download, Loader2 } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogFooter, DialogTitle } from '@/components/ui/dialog'
import { Select } from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { ApiError, resizeAttachmentImage } from '@/lib/api'
import type { ImageEditResultResponse, ImageResizeFit, ImageResizeOutputFormat, ImageResizePresetOut } from '@/lib/types'

function downloadDataUrl(dataUrl: string, filename: string): void {
  const link = document.createElement('a')
  link.href = dataUrl
  link.download = filename
  link.click()
}

/** "Resize" -- capability (C) in CLAUDE.md's "four capabilities" split:
 * deterministic Pillow work (`POST /attachments/{id}/resize`), no model
 * call. Always produces a brand-new attachment; the original is never
 * mutated. `onResized`, if the user chooses "Attach resized image", hands
 * the *new* attachment id up to the composer -- so if this is then sent as
 * part of a question, the resized bytes (not the original) are what reach
 * the model. */
export function ResizeImageDialog({
  open,
  onOpenChange,
  attachmentId,
  filename,
  previewUrl,
  presets,
  maxDimension,
  onResized,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  attachmentId: string
  filename: string
  previewUrl: string | null
  presets: ImageResizePresetOut[]
  maxDimension: number
  onResized: (result: ImageEditResultResponse) => void
}) {
  const { t } = useTranslation()
  const [width, setWidth] = useState('')
  const [height, setHeight] = useState('')
  const [lockAspect, setLockAspect] = useState(true)
  const [fit, setFit] = useState<ImageResizeFit>('contain')
  const [outputFormat, setOutputFormat] = useState<ImageResizeOutputFormat | ''>('')
  const [naturalSize, setNaturalSize] = useState<{ width: number; height: number } | null>(null)
  const [status, setStatus] = useState<'idle' | 'applying' | 'succeeded' | 'error'>('idle')
  const [result, setResult] = useState<ImageEditResultResponse | null>(null)
  const [errorMessage, setErrorMessage] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    setWidth('')
    setHeight('')
    setLockAspect(true)
    setFit('contain')
    setOutputFormat('')
    setStatus('idle')
    setResult(null)
    setErrorMessage(null)
  }, [open, attachmentId])

  useEffect(() => {
    if (!open || !previewUrl) return
    const img = new Image()
    img.onload = () => setNaturalSize({ width: img.naturalWidth, height: img.naturalHeight })
    img.src = previewUrl
  }, [open, previewUrl])

  const computedHeight =
    lockAspect && naturalSize && width
      ? Math.max(1, Math.round((Number(width) * naturalSize.height) / naturalSize.width))
      : null

  const applyPreset = (preset: ImageResizePresetOut) => {
    setLockAspect(false)
    setWidth(String(preset.width))
    setHeight(String(preset.height))
    setFit('cover')
  }

  const handleApply = async () => {
    const widthValue = width ? Number(width) : undefined
    const heightValue = lockAspect ? undefined : height ? Number(height) : undefined
    if (!widthValue && !heightValue) {
      setErrorMessage(t('attachments.resize.errorNoDimension'))
      setStatus('error')
      return
    }
    setStatus('applying')
    setErrorMessage(null)
    try {
      const response = await resizeAttachmentImage(attachmentId, {
        width: widthValue,
        height: heightValue,
        fit,
        output_format: outputFormat || undefined,
      })
      setResult(response)
      setStatus('succeeded')
    } catch (error) {
      setErrorMessage(error instanceof ApiError ? error.message : t('attachments.resize.errorGeneric'))
      setStatus('error')
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogTitle>{t('attachments.resize.title')}</DialogTitle>
        <p className="mb-3 truncate text-xs text-[var(--muted-foreground)]" title={filename}>
          {filename}
          {naturalSize && ` -- ${naturalSize.width} x ${naturalSize.height}px`}
        </p>

        {status === 'succeeded' && result ? (
          <div className="flex flex-col gap-3">
            {result.image_data_url && (
              <img
                src={result.image_data_url}
                alt={t('attachments.resize.resultLabel')}
                className="mx-auto max-h-64 rounded-md border border-[var(--border)] object-contain"
              />
            )}
            <p className="text-center text-xs text-[var(--muted-foreground)]">
              {result.width} x {result.height}px -- {(result.size_bytes / 1024).toFixed(0)} KB
            </p>
            <DialogFooter className="mt-0 justify-center">
              <Button variant="secondary" onClick={() => downloadDataUrl(result.image_data_url, filename)}>
                <Download className="h-4 w-4" />
                {t('attachments.resize.downloadResult')}
              </Button>
              <Button
                onClick={() => {
                  onResized(result)
                  onOpenChange(false)
                }}
              >
                {t('attachments.resize.attachResult')}
              </Button>
            </DialogFooter>
          </div>
        ) : (
          <div className="flex flex-col gap-3">
            {presets.length > 0 && (
              <div className="flex flex-col gap-1.5">
                <span className="text-xs font-medium text-[var(--muted-foreground)]">
                  {t('attachments.resize.presets')}
                </span>
                <div className="flex flex-wrap gap-1.5">
                  {presets.map((preset) => (
                    <Button key={preset.name} size="sm" variant="secondary" onClick={() => applyPreset(preset)}>
                      {preset.width}x{preset.height}
                    </Button>
                  ))}
                </div>
              </div>
            )}

            <div className="grid grid-cols-2 gap-3">
              <label className="flex flex-col gap-1 text-xs">
                <span className="font-medium text-[var(--muted-foreground)]">
                  {t('attachments.resize.widthLabel')}
                </span>
                <input
                  type="number"
                  min={1}
                  max={maxDimension}
                  value={width}
                  onChange={(e) => setWidth(e.target.value)}
                  className="h-9 rounded-md border border-[var(--border)] bg-[var(--input)] px-2.5 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
                />
              </label>
              <label className="flex flex-col gap-1 text-xs">
                <span className="font-medium text-[var(--muted-foreground)]">
                  {t('attachments.resize.heightLabel')}
                </span>
                <input
                  type="number"
                  min={1}
                  max={maxDimension}
                  value={lockAspect ? (computedHeight ?? '') : height}
                  disabled={lockAspect}
                  onChange={(e) => setHeight(e.target.value)}
                  className="h-9 rounded-md border border-[var(--border)] bg-[var(--input)] px-2.5 text-sm disabled:opacity-60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
                />
              </label>
            </div>

            <label className="flex items-center justify-between gap-2 rounded-md border border-[var(--border)] px-2.5 py-1.5 text-xs">
              <span className="font-medium text-[var(--muted-foreground)]">
                {t('attachments.resize.lockAspect')}
              </span>
              <Switch checked={lockAspect} onCheckedChange={setLockAspect} />
            </label>

            {!lockAspect && (
              <label className="flex flex-col gap-1 text-xs">
                <span className="font-medium text-[var(--muted-foreground)]">
                  {t('attachments.resize.fitLabel')}
                </span>
                <Select value={fit} onChange={(e) => setFit(e.target.value as ImageResizeFit)}>
                  <option value="contain">{t('attachments.resize.fitContain')}</option>
                  <option value="cover">{t('attachments.resize.fitCover')}</option>
                  <option value="stretch">{t('attachments.resize.fitStretch')}</option>
                </Select>
              </label>
            )}

            <label className="flex flex-col gap-1 text-xs">
              <span className="font-medium text-[var(--muted-foreground)]">
                {t('attachments.resize.formatLabel')}
              </span>
              <Select
                value={outputFormat}
                onChange={(e) => setOutputFormat(e.target.value as ImageResizeOutputFormat | '')}
              >
                <option value="">{t('attachments.resize.formatKeep')}</option>
                <option value="png">PNG</option>
                <option value="jpeg">JPEG</option>
                <option value="webp">WEBP</option>
              </Select>
            </label>

            {status === 'error' && errorMessage && (
              <div className="flex items-start gap-2 rounded-md border border-[var(--danger)]/30 bg-[var(--danger)]/10 p-2.5 text-xs text-[var(--danger)]">
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                <span>{errorMessage}</span>
              </div>
            )}

            <DialogFooter className="mt-0">
              <Button onClick={() => void handleApply()} disabled={status === 'applying'}>
                {status === 'applying' ? (
                  <>
                    <Loader2 className="h-4 w-4 animate-spin" />
                    {t('attachments.resize.applying')}
                  </>
                ) : (
                  t('attachments.resize.apply')
                )}
              </Button>
            </DialogFooter>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}
