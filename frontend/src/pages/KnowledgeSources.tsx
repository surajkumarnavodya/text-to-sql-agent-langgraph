import { Download, Trash2, UploadCloud } from 'lucide-react'
import { useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Button } from '@/components/ui/button'
import { Select } from '@/components/ui/select'
import { useDeleteDocument, useDocuments, useUploadDocument } from '@/hooks/queries'
import { ApiError, downloadDocument } from '@/lib/api'
import type { Collection, DocumentOut, SensitivityCategory } from '@/lib/types'

const STATUS_ICON: Record<DocumentOut['status'], string> = { ready: '✅', processing: '⏳', failed: '❌' }

export function KnowledgeSources() {
  const { t } = useTranslation()

  return (
    // Single full-width scroll region (matches Chat.tsx / ChatGPT's
    // layout) -- <main> in AppShell no longer scrolls itself, so each
    // routed page owns exactly one scrollbar, flush against the browser's
    // right edge, with content centered inside via max-w-4xl/mx-auto.
    <div className="h-full min-h-0 overflow-y-auto">
      <div className="mx-auto max-w-4xl px-4 py-6">
        <h1 className="text-xl font-bold">📚 {t('knowledge.title')}</h1>
        <p className="mt-1 text-sm text-[var(--muted-foreground)]">{t('knowledge.subtitle')}</p>

        <Tabs defaultValue="documents" className="mt-6">
          <TabsList>
            <TabsTrigger value="documents">📄 {t('knowledge.documents')}</TabsTrigger>
            <TabsTrigger value="policies">🔒 {t('knowledge.policies')}</TabsTrigger>
          </TabsList>
          <TabsContent value="documents">
            <CollectionPanel collection="documents" />
          </TabsContent>
          <TabsContent value="policies">
            <CollectionPanel collection="policies" />
          </TabsContent>
        </Tabs>
      </div>
    </div>
  )
}

const SENSITIVITY_OPTIONS: { value: SensitivityCategory; labelKey: string }[] = [
  { value: null, labelKey: 'knowledge.none' },
  { value: 'compensation', labelKey: 'knowledge.compensation' },
  { value: 'disciplinary', labelKey: 'knowledge.disciplinary' },
  { value: 'legal', labelKey: 'knowledge.legal' },
]

function CollectionPanel({ collection }: { collection: Collection }) {
  const { t } = useTranslation()
  const documents = useDocuments(collection)
  const upload = useUploadDocument(collection)
  const deleteMutation = useDeleteDocument()
  const fileInput = useRef<HTMLInputElement>(null)
  const [sensitivity, setSensitivity] = useState<SensitivityCategory>(null)
  const [uploadError, setUploadError] = useState<string | null>(null)

  const handleUpload = async () => {
    const file = fileInput.current?.files?.[0]
    if (!file) return
    setUploadError(null)
    try {
      await upload.mutateAsync({ file, sensitivityCategory: sensitivity })
      if (fileInput.current) fileInput.current.value = ''
    } catch (error) {
      setUploadError(error instanceof ApiError ? error.message : 'Upload failed.')
    }
  }

  if (documents.isError && documents.error instanceof ApiError && documents.error.status === 404) {
    return <p className="mt-4 text-sm text-[var(--muted-foreground)]">{t('knowledge.disabled')}</p>
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="rounded-lg border border-[var(--border)] bg-[var(--card)] p-4">
        <h2 className="mb-3 text-sm font-semibold">{t('knowledge.upload')}</h2>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
          <div className="flex-1">
            <input
              ref={fileInput}
              type="file"
              accept="application/pdf"
              className="block w-full text-sm file:mr-3 file:rounded-md file:border-0 file:bg-[var(--muted)] file:px-3 file:py-1.5 file:text-sm"
            />
          </div>
          {collection === 'policies' && (
            <div className="flex flex-col gap-1">
              <label className="text-xs text-[var(--muted-foreground)]">{t('knowledge.sensitivity')}</label>
              <Select
                value={sensitivity ?? ''}
                onChange={(event) => setSensitivity((event.target.value || null) as SensitivityCategory)}
              >
                {SENSITIVITY_OPTIONS.map((option) => (
                  <option key={option.labelKey} value={option.value ?? ''}>
                    {t(option.labelKey)}
                  </option>
                ))}
              </Select>
            </div>
          )}
          <Button variant="primary" onClick={() => void handleUpload()} disabled={upload.isPending}>
            <UploadCloud className="h-4 w-4" />
            {t('knowledge.ingest')}
          </Button>
        </div>
        {uploadError && <p className="mt-2 text-xs text-[var(--danger)]">{uploadError}</p>}
        {upload.data && (
          <p className="mt-2 text-xs text-[var(--muted-foreground)]">
            {upload.data.status === 'ready'
              ? `${upload.data.filename}: ${upload.data.chunk_count} ${t('common.chunks')} indexed.`
              : `${upload.data.filename}: failed -- ${upload.data.error_message}`}
          </p>
        )}
      </div>

      <div>
        <h2 className="mb-3 text-sm font-semibold">{t('knowledge.ingestedDocuments')}</h2>
        {(documents.data?.documents.length ?? 0) === 0 ? (
          <p className="text-sm text-[var(--muted-foreground)]">{t('knowledge.nothingIngested')}</p>
        ) : (
          <div className="flex flex-col gap-2">
            {documents.data?.documents.map((doc) => (
              <div
                key={doc.id}
                className="flex flex-wrap items-center gap-3 rounded-md border border-[var(--border)] bg-[var(--card)] p-2.5 text-sm"
              >
                <span>{STATUS_ICON[doc.status]}</span>
                <span className="flex-1 font-medium">{doc.filename}</span>
                <span className="text-xs text-[var(--muted-foreground)]">{doc.upload_date}</span>
                <span className="text-xs text-[var(--muted-foreground)]">
                  {doc.chunk_count} {t('common.chunks')}
                </span>
                <span className="text-xs text-[var(--muted-foreground)]">{doc.sensitivity_category ?? '—'}</span>
                {doc.has_pdf_bytes && (
                  <Button
                    size="icon"
                    variant="ghost"
                    aria-label={t('common.download')}
                    onClick={() => void downloadDocument(doc.id, doc.filename)}
                  >
                    <Download className="h-4 w-4" />
                  </Button>
                )}
                <Button
                  size="icon"
                  variant="ghost"
                  aria-label={t('common.delete')}
                  onClick={() => deleteMutation.mutate(doc.id)}
                >
                  <Trash2 className="h-4 w-4" />
                </Button>
                {doc.status === 'failed' && doc.error_message && (
                  <p className="w-full text-xs text-[var(--danger)]">⚠️ {doc.error_message}</p>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
