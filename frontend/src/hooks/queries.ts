import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  deleteDocument,
  getAttachmentCapabilities,
  getAvailableModels,
  getHealth,
  getSchemaTables,
  listDocuments,
  refreshSchema,
  uploadDocument,
} from '@/lib/api'
import {
  cancelOnboardingJob,
  createOnboardingJob,
  decideOnboardingReviewItem,
  getOnboardingJob,
  listOnboardingArtifacts,
  listOnboardingJobs,
  listOnboardingReviewItems,
  publishOnboardingJob,
  retryOnboardingJob,
  runOnboardingDiscovery,
} from '@/lib/onboardingApi'
import {
  createCatalogEntry,
  getCatalogEntry,
  getCatalogEntryVersions,
  listCatalogEntries,
  publishCatalogEntry,
  requestCatalogEntryChanges,
  reviewCatalogEntry,
  updateCatalogEntry,
} from '@/lib/semanticCatalogApi'
import type {
  Collection,
  CreateCatalogEntryRequest,
  DecideReviewItemRequest,
  PublishJobRequest,
  ReviewDecisionRequest,
  RunDiscoveryRequest,
  SensitivityCategory,
  UpdateCatalogEntryRequest,
} from '@/lib/types'

export function useHealth() {
  return useQuery({ queryKey: ['health'], queryFn: getHealth, staleTime: 30_000 })
}

/** What this deployment can actually do with an attachment right now
 * (vision/OCR/resize/text-removal) -- see AttachmentCapabilities's own
 * docstring. Cached the same 30s as useHealth above, since it changes only
 * with server config, never per-request. */
export function useAttachmentCapabilities() {
  return useQuery({
    queryKey: ['attachment-capabilities'],
    queryFn: getAttachmentCapabilities,
    staleTime: 30_000,
  })
}

/** The Ollama Text-to-SQL model registry (GET /models) -- which models are
 * configured/allowed and which are actually installed locally right now.
 * Cached the same 30s as useHealth above: model availability changes only
 * with server config/local `ollama pull` state, never per-request, and the
 * backend must not be re-queried on every question. */
export function useAvailableModels() {
  return useQuery({
    queryKey: ['models'],
    queryFn: getAvailableModels,
    staleTime: 30_000,
  })
}

export function useSchemaTables() {
  return useQuery({ queryKey: ['schema-tables'], queryFn: () => getSchemaTables() })
}

export function useRefreshSchema() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: refreshSchema,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['schema-tables'] })
    },
  })
}

export function useDocuments(collection?: Collection) {
  return useQuery({
    queryKey: ['documents', collection ?? 'all'],
    queryFn: () => listDocuments(collection),
  })
}

export function useUploadDocument(collection: Collection) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ file, sensitivityCategory }: { file: File; sensitivityCategory?: SensitivityCategory }) =>
      uploadDocument(file, collection, sensitivityCategory ?? undefined),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['documents'] })
    },
  })
}

export function useDeleteDocument() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: deleteDocument,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['documents'] })
    },
  })
}

// --- Client-database onboarding (onboarding/, api/onboarding.py, Prompt 26) ---
// No `refetchInterval`/polling anywhere below: `POST .../discover` and
// `POST .../publish` are synchronous backend calls (no background worker,
// see onboarding/jobs.py's own module docstring for why) -- each mutation's
// own response already carries the job's final status, so there is no
// in-between state a poll would ever observe that the mutation result
// doesn't already have.

export function useOnboardingJobs() {
  return useQuery({ queryKey: ['onboarding-jobs'], queryFn: listOnboardingJobs })
}

export function useOnboardingJob(jobId: string | null) {
  return useQuery({
    queryKey: ['onboarding-job', jobId],
    queryFn: () => getOnboardingJob(jobId as string),
    enabled: jobId !== null,
  })
}

export function useOnboardingReviewItems(jobId: string | null) {
  return useQuery({
    queryKey: ['onboarding-review-items', jobId],
    queryFn: () => listOnboardingReviewItems(jobId as string),
    enabled: jobId !== null,
  })
}

export function useOnboardingArtifacts(jobId: string | null) {
  return useQuery({
    queryKey: ['onboarding-artifacts', jobId],
    queryFn: () => listOnboardingArtifacts(jobId as string),
    enabled: jobId !== null,
  })
}

export function useCreateOnboardingJob() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: createOnboardingJob,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['onboarding-jobs'] })
    },
  })
}

export function useRunOnboardingDiscovery(jobId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: RunDiscoveryRequest) => runOnboardingDiscovery(jobId, payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['onboarding-job', jobId] })
      void queryClient.invalidateQueries({ queryKey: ['onboarding-jobs'] })
      void queryClient.invalidateQueries({ queryKey: ['onboarding-review-items', jobId] })
    },
  })
}

export function useDecideOnboardingReviewItem(jobId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ itemId, payload }: { itemId: string; payload: DecideReviewItemRequest }) =>
      decideOnboardingReviewItem(jobId, itemId, payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['onboarding-review-items', jobId] })
    },
  })
}

export function usePublishOnboardingJob(jobId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: PublishJobRequest) => publishOnboardingJob(jobId, payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['onboarding-job', jobId] })
      void queryClient.invalidateQueries({ queryKey: ['onboarding-jobs'] })
      void queryClient.invalidateQueries({ queryKey: ['onboarding-artifacts', jobId] })
    },
  })
}

export function useCancelOnboardingJob(jobId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => cancelOnboardingJob(jobId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['onboarding-job', jobId] })
      void queryClient.invalidateQueries({ queryKey: ['onboarding-jobs'] })
    },
  })
}

export function useRetryOnboardingJob(jobId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => retryOnboardingJob(jobId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['onboarding-job', jobId] })
      void queryClient.invalidateQueries({ queryKey: ['onboarding-jobs'] })
    },
  })
}

// --- Tenant-aware semantic catalog (semantic/catalog.py, api/semantic_catalog.py,
// Prompt 09/10/27 -- the SME Semantic Review Dashboard) ---

export function useCatalogEntries(params?: {
  databaseId?: string
  conceptType?: string
  status?: string
  includeConflicts?: boolean
}) {
  return useQuery({
    queryKey: ['catalog-entries', params ?? null],
    queryFn: () => listCatalogEntries(params),
  })
}

export function useCatalogEntry(entryId: string | null) {
  return useQuery({
    queryKey: ['catalog-entry', entryId],
    queryFn: () => getCatalogEntry(entryId as string),
    enabled: entryId !== null,
  })
}

export function useCatalogEntryVersions(
  params: { conceptKey: string; databaseId: string; conceptType: string } | null,
) {
  return useQuery({
    queryKey: ['catalog-entry-versions', params],
    queryFn: () =>
      getCatalogEntryVersions(params as { conceptKey: string; databaseId: string; conceptType: string }),
    enabled: params !== null,
  })
}

export function useCreateCatalogEntry() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: CreateCatalogEntryRequest) => createCatalogEntry(payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['catalog-entries'] })
    },
  })
}

function useInvalidateCatalogQueries(entryId: string) {
  const queryClient = useQueryClient()
  return () => {
    void queryClient.invalidateQueries({ queryKey: ['catalog-entry', entryId] })
    void queryClient.invalidateQueries({ queryKey: ['catalog-entries'] })
  }
}

export function useUpdateCatalogEntry(entryId: string) {
  const invalidate = useInvalidateCatalogQueries(entryId)
  return useMutation({
    mutationFn: (payload: UpdateCatalogEntryRequest) => updateCatalogEntry(entryId, payload),
    onSuccess: invalidate,
  })
}

export function useReviewCatalogEntry(entryId: string) {
  const invalidate = useInvalidateCatalogQueries(entryId)
  return useMutation({
    mutationFn: (payload: ReviewDecisionRequest) => reviewCatalogEntry(entryId, payload),
    onSuccess: invalidate,
  })
}

export function useRequestCatalogEntryChanges(entryId: string) {
  const invalidate = useInvalidateCatalogQueries(entryId)
  return useMutation({
    mutationFn: (payload: ReviewDecisionRequest) => requestCatalogEntryChanges(entryId, payload),
    onSuccess: invalidate,
  })
}

export function usePublishCatalogEntry(entryId: string) {
  const invalidate = useInvalidateCatalogQueries(entryId)
  return useMutation({
    mutationFn: () => publishCatalogEntry(entryId),
    onSuccess: invalidate,
  })
}
