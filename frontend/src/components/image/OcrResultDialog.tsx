import { AlertTriangle, Loader2 } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { CopyButton } from '@/components/ui/copy-button'
import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog'
import { ApiError, extractAttachmentText } from '@/lib/api'
import type { OcrExtractResponse } from '@/lib/types'

type Status = 'loading' | 'succeeded' | 'error'

/** "Extract text" -- capability (B) in CLAUDE.md's "four capabilities"
 * split: a real OCR pass (`POST /attachments/{id}/extract-text`), never a
 * vision-model paraphrase. Shows Tesseract's own recognized text exactly as
 * returned (never rewritten), plus any warnings (low confidence, nothing
 * detected, OCR unavailable) surfaced honestly rather than hidden. */
export function OcrResultDialog({
  open,
  onOpenChange,
  attachmentId,
  filename,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  attachmentId: string
  filename: string
}) {
  const { t } = useTranslation()
  const [status, setStatus] = useState<Status>('loading')
  const [result, setResult] = useState<OcrExtractResponse | null>(null)
  const [errorMessage, setErrorMessage] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    let cancelled = false
    setStatus('loading')
    setResult(null)
    setErrorMessage(null)
    void extractAttachmentText(attachmentId)
      .then((response) => {
        if (cancelled) return
        setResult(response)
        setStatus('succeeded')
      })
      .catch((error: unknown) => {
        if (cancelled) return
        setErrorMessage(error instanceof ApiError ? error.message : t('attachments.ocr.errorGeneric'))
        setStatus('error')
      })
    return () => {
      cancelled = true
    }
  }, [open, attachmentId, t])

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogTitle>{t('attachments.ocr.title')}</DialogTitle>
        <p className="mb-3 truncate text-xs text-[var(--muted-foreground)]" title={filename}>
          {filename}
        </p>

        {status === 'loading' && (
          <div className="flex items-center justify-center gap-2 py-10 text-sm text-[var(--muted-foreground)]">
            <Loader2 className="h-4 w-4 animate-spin" />
            {t('attachments.ocr.loading')}
          </div>
        )}

        {status === 'error' && (
          <div className="flex items-start gap-2 rounded-md border border-[var(--danger)]/30 bg-[var(--danger)]/10 p-3 text-sm text-[var(--danger)]">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <span>{errorMessage}</span>
          </div>
        )}

        {status === 'succeeded' && result && (
          <div className="flex flex-col gap-3">
            {result.warnings.map((warning) => (
              <div
                key={warning}
                className="flex items-start gap-2 rounded-md border border-[var(--warning,#d97706)]/30 bg-[var(--warning,#d97706)]/10 p-2.5 text-xs text-[var(--warning,#d97706)]"
              >
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                <span>{warning}</span>
              </div>
            ))}

            {result.raw_text ? (
              <div className="flex flex-col gap-1.5">
                <div className="flex items-center justify-between">
                  <span className="text-xs font-medium text-[var(--muted-foreground)]">
                    {t('attachments.ocr.rawTextLabel')}
                  </span>
                  <CopyButton getText={() => result.raw_text} label={t('attachments.ocr.copyText')} />
                </div>
                <pre className="max-h-80 overflow-auto whitespace-pre-wrap rounded-md border border-[var(--border)] bg-[var(--muted)] p-3 font-mono text-xs leading-relaxed">
                  {result.raw_text}
                </pre>
              </div>
            ) : (
              !result.warnings.length && (
                <p className="py-6 text-center text-sm text-[var(--muted-foreground)]">
                  {t('attachments.ocr.noTextFound')}
                </p>
              )
            )}
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}
