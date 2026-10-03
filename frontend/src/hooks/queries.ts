import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { getNavigation } from '@/lib/navigationApi'
import { useLocalAuthStore } from '@/store/localAuthStore'
import {
  addRecommendationNote,
  assignRecommendationOwner,
  expireRecommendation,
  listRecommendationEvents,
  listRecommendations,
  resolveRecommendation,
  submitRecommendationVerdict,
  type RecommendationListFilters,
} from '@/lib/recommendationApi'
import {
  deleteDocument,
  getAttachmentCapabilities,
  getAvailableModels,
  getHealth,
  getPerformanceMetrics,
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
  assignUserRole,
  createTenant,
  getConfigStatus,
  getPlatformMetrics,
  getSemanticReviewQueue,
  listAuditLogs,
  listPlatformDatabases,
  listPlatformJobs,
  listPlatformUsers,
  listRoles,
  listSecurityEvents,
  listTenants,
  removeUserRole,
  setTenantStatus,
} from '@/lib/platformAdminApi'
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
import {
  assignTenantUserRole,
  getTenantPendingReviews,
  getTenantProfile,
  getTenantRecommendationQualityMetrics,
  getTenantSemanticCatalogStatus,
  listTenantAssignableRoles,
  listTenantAuditEvents,
  listTenantDatabases,
  listTenantEvaluationResults,
  listTenantGoldenQuestions,
  listTenantRecommendations,
  listTenantUsers,
  refreshTenantDatabases,
  removeTenantUserRole,
} from '@/lib/tenantAdminApi'
import type {
  AssignRoleRequest,
  Collection,
  CreateCatalogEntryRequest,
  CreateTenantRequest,
  DecideReviewItemRequest,
  PublishJobRequest,
  ReviewDecisionRequest,
  RunDiscoveryRequest,
  SensitivityCategory,
  SetTenantStatusRequest,
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

// --- Global platform admin dashboard (api/platform_admin.py, Prompt 28) ---
// No `refetchInterval` anywhere below -- this is an operator dashboard a
// human opens and reads, not a live-polling view; React Query's own
// manual `refetch`/`invalidateQueries` after a mutation is enough.

export function usePlatformTenants() {
  return useQuery({ queryKey: ['platform-admin-tenants'], queryFn: listTenants })
}

export function useCreateTenant() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: CreateTenantRequest) => createTenant(payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['platform-admin-tenants'] })
    },
  })
}

export function useSetTenantStatus(tenantId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: SetTenantStatusRequest) => setTenantStatus(tenantId, payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['platform-admin-tenants'] })
      void queryClient.invalidateQueries({ queryKey: ['platform-admin-audit-logs'] })
    },
  })
}

export function usePlatformUsers(params?: { tenantId?: string; roleName?: string; status?: string }) {
  return useQuery({
    queryKey: ['platform-admin-users', params ?? null],
    queryFn: () => listPlatformUsers(params),
  })
}

export function useAssignUserRole(userId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: AssignRoleRequest) => assignUserRole(userId, payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['platform-admin-users'] })
      void queryClient.invalidateQueries({ queryKey: ['platform-admin-audit-logs'] })
    },
  })
}

export function useRemoveUserRole(userId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (roleName: string) => removeUserRole(userId, roleName),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['platform-admin-users'] })
      void queryClient.invalidateQueries({ queryKey: ['platform-admin-audit-logs'] })
    },
  })
}

export function usePlatformRoles() {
  return useQuery({ queryKey: ['platform-admin-roles'], queryFn: listRoles })
}

export function usePlatformDatabases() {
  return useQuery({ queryKey: ['platform-admin-databases'], queryFn: listPlatformDatabases })
}

export function useSemanticReviewQueue() {
  return useQuery({
    queryKey: ['platform-admin-semantic-review-queue'],
    queryFn: getSemanticReviewQueue,
  })
}

export function usePlatformJobs(status?: string) {
  return useQuery({
    queryKey: ['platform-admin-jobs', status ?? null],
    queryFn: () => listPlatformJobs(status),
  })
}

export function usePlatformMetrics() {
  return useQuery({ queryKey: ['platform-admin-metrics'], queryFn: getPlatformMetrics })
}

export function useSecurityEvents(params?: { limit?: number; severity?: string; eventType?: string }) {
  return useQuery({
    queryKey: ['platform-admin-security-events', params ?? null],
    queryFn: () => listSecurityEvents(params),
  })
}

export function useAuditLogs(params?: { action?: string; resourceType?: string; outcome?: string }) {
  return useQuery({
    queryKey: ['platform-admin-audit-logs', params ?? null],
    queryFn: () => listAuditLogs(params),
  })
}

export function useConfigStatus() {
  return useQuery({ queryKey: ['platform-admin-config-status'], queryFn: getConfigStatus })
}

// --- Tenant/client admin dashboard (api/tenant_admin.py, Prompt 29) ---

export function useTenantProfile() {
  return useQuery({ queryKey: ['tenant-admin-profile'], queryFn: getTenantProfile })
}

export function useTenantDatabases() {
  return useQuery({ queryKey: ['tenant-admin-databases'], queryFn: listTenantDatabases })
}

export function useRefreshTenantDatabases() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: refreshTenantDatabases,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['tenant-admin-databases'] })
    },
  })
}

export function useTenantUsers() {
  return useQuery({ queryKey: ['tenant-admin-users'], queryFn: listTenantUsers })
}

export function useAssignTenantUserRole(userId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: AssignRoleRequest) => assignTenantUserRole(userId, payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['tenant-admin-users'] })
    },
  })
}

export function useRemoveTenantUserRole(userId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (roleName: string) => removeTenantUserRole(userId, roleName),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['tenant-admin-users'] })
    },
  })
}

export function useTenantAssignableRoles() {
  return useQuery({ queryKey: ['tenant-admin-roles'], queryFn: listTenantAssignableRoles })
}

export function useTenantSemanticCatalogStatus() {
  return useQuery({
    queryKey: ['tenant-admin-semantic-catalog-status'],
    queryFn: getTenantSemanticCatalogStatus,
  })
}

export function useTenantPendingReviews() {
  return useQuery({ queryKey: ['tenant-admin-pending-reviews'], queryFn: getTenantPendingReviews })
}

export function useTenantGoldenQuestions() {
  return useQuery({ queryKey: ['tenant-admin-golden-questions'], queryFn: listTenantGoldenQuestions })
}

export function useTenantEvaluationResults() {
  return useQuery({
    queryKey: ['tenant-admin-evaluation'],
    queryFn: listTenantEvaluationResults,
  })
}

export function useTenantAuditEvents() {
  return useQuery({ queryKey: ['tenant-admin-audit'], queryFn: listTenantAuditEvents })
}

export function useTenantRecommendations() {
  return useQuery({ queryKey: ['tenant-admin-recommendations'], queryFn: listTenantRecommendations })
}

export function useTenantRecommendationQualityMetrics() {
  return useQuery({
    queryKey: ['tenant-admin-recommendation-metrics'],
    queryFn: getTenantRecommendationQualityMetrics,
  })
}

/** `GET /metrics/performance` -- already tenant-scoped server-side (see
 * `lib/api.ts::getPerformanceMetrics`'s own docstring), reused directly
 * rather than duplicated under `/tenant-admin/*`. */
export function useTenantPerformanceMetrics() {
  return useQuery({ queryKey: ['tenant-admin-performance-metrics'], queryFn: getPerformanceMetrics })
}

// --- Role-based navigation (Prompt 32, `api/navigation.py`). The server decides
// every screen and action; nothing here reads a role name. Keyed by the local
// user id when there is one, and by a session key otherwise (OIDC, static token,
// auth off), so a different person on the same browser never reuses another's
// entry. `staleTime: 0` refetches on every mount: a changed role or a suspended
// tenant must take effect immediately, not after a cache window. ---

export function useNavigation() {
  const userKey = useLocalAuthStore((state) => state.user?.id ?? 'session')
  return useQuery({
    queryKey: ['navigation', userKey],
    queryFn: getNavigation,
    staleTime: 0,
    retry: false,
  })
}

/** The server's named-action map for this caller (`security/navigation.py`'s
 * `CAPABILITY_RULES`). Empty until navigation loads, so every action is hidden
 * by default rather than shown on a guess. */
export function useCapabilities(): Record<string, boolean> {
  return useNavigation().data?.capabilities ?? {}
}

// --- Recommendation & action dashboard (Prompt 31,
// `frontend/src/pages/Recommendations.tsx`). No `refetchInterval` -- a
// reviewer acts on a record and the mutations below invalidate what changed. ---

export function useRecommendations(filters: RecommendationListFilters = {}) {
  return useQuery({
    queryKey: ['recommendations', filters],
    queryFn: () => listRecommendations(filters),
  })
}

export function useRecommendationEvents(recordId: string | null) {
  return useQuery({
    queryKey: ['recommendation-events', recordId],
    queryFn: () => listRecommendationEvents(recordId as string),
    enabled: recordId !== null,
  })
}

/** Every recommendation action changes the list (status/owner) and the
 * selected record's audit trail, so all of them invalidate both. */
function useInvalidateRecommendationQueries() {
  const queryClient = useQueryClient()
  return () => {
    void queryClient.invalidateQueries({ queryKey: ['recommendations'] })
    void queryClient.invalidateQueries({ queryKey: ['recommendation-events'] })
    void queryClient.invalidateQueries({ queryKey: ['tenant-admin-recommendation-metrics'] })
  }
}

export function useSubmitRecommendationVerdict() {
  const invalidate = useInvalidateRecommendationQueries()
  return useMutation({
    mutationFn: ({
      recordId,
      status,
      reason,
    }: {
      recordId: string
      status: Parameters<typeof submitRecommendationVerdict>[1]['status']
      reason?: string | null
    }) => submitRecommendationVerdict(recordId, { status, reason: reason ?? null }),
    onSuccess: invalidate,
  })
}

export function useResolveRecommendation() {
  const invalidate = useInvalidateRecommendationQueries()
  return useMutation({
    mutationFn: ({ recordId, reason }: { recordId: string; reason?: string | null }) =>
      resolveRecommendation(recordId, reason ?? null),
    onSuccess: invalidate,
  })
}

export function useExpireRecommendation() {
  const invalidate = useInvalidateRecommendationQueries()
  return useMutation({
    mutationFn: ({ recordId, reason }: { recordId: string; reason?: string | null }) =>
      expireRecommendation(recordId, reason ?? null),
    onSuccess: invalidate,
  })
}

export function useAddRecommendationNote() {
  const invalidate = useInvalidateRecommendationQueries()
  return useMutation({
    mutationFn: ({ recordId, note }: { recordId: string; note: string }) =>
      addRecommendationNote(recordId, note),
    onSuccess: invalidate,
  })
}

export function useAssignRecommendationOwner() {
  const invalidate = useInvalidateRecommendationQueries()
  return useMutation({
    mutationFn: ({ recordId, ownerUserId }: { recordId: string; ownerUserId: string | null }) =>
      assignRecommendationOwner(recordId, ownerUserId),
    onSuccess: invalidate,
  })
}
