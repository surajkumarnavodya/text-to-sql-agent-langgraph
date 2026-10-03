import { request } from './api'
import type {
  AssignRoleRequest,
  DatabaseStatusOut,
  EvaluationSummaryOut,
  GoldenQuestionsSummaryOut,
  PendingReviewsOut,
  PlatformUserOut,
  RecommendationQualityMetricsOut,
  RecommendationRecordOut,
  RoleOut,
  SchemaRefreshResponse,
  SecurityEventOut,
  SemanticCatalogStatusOut,
  TenantOut,
} from './types'

/** Thin wrappers around `/tenant-admin/*` (`api/tenant_admin.py`, Prompt
 * 29) -- mirrors `lib/platformAdminApi.ts`'s own established convention
 * exactly. Every function below takes no `tenantId` parameter at all --
 * the server resolves it from the caller's own account; there is no
 * client-side way to ask for another tenant's data (see
 * `api/tenant_admin.py`'s own docstring). */

export function getTenantProfile(): Promise<TenantOut> {
  return request<TenantOut>('/tenant-admin/profile')
}

export function listTenantDatabases(): Promise<DatabaseStatusOut[]> {
  return request<DatabaseStatusOut[]>('/tenant-admin/databases')
}

export function refreshTenantDatabases(): Promise<SchemaRefreshResponse> {
  return request<SchemaRefreshResponse>('/tenant-admin/databases/refresh', { method: 'POST' })
}

export function listTenantUsers(): Promise<PlatformUserOut[]> {
  return request<PlatformUserOut[]>('/tenant-admin/users')
}

export function assignTenantUserRole(
  userId: string,
  payload: AssignRoleRequest,
): Promise<PlatformUserOut> {
  return request<PlatformUserOut>(`/tenant-admin/users/${userId}/roles`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function removeTenantUserRole(userId: string, roleName: string): Promise<PlatformUserOut> {
  return request<PlatformUserOut>(`/tenant-admin/users/${userId}/roles/${roleName}`, {
    method: 'DELETE',
  })
}

export function listTenantAssignableRoles(): Promise<RoleOut[]> {
  return request<RoleOut[]>('/tenant-admin/roles')
}

export function getTenantSemanticCatalogStatus(): Promise<SemanticCatalogStatusOut> {
  return request<SemanticCatalogStatusOut>('/tenant-admin/semantic-catalog-status')
}

export function getTenantPendingReviews(): Promise<PendingReviewsOut> {
  return request<PendingReviewsOut>('/tenant-admin/pending-reviews')
}

export function listTenantGoldenQuestions(): Promise<GoldenQuestionsSummaryOut[]> {
  return request<GoldenQuestionsSummaryOut[]>('/tenant-admin/golden-questions')
}

export function listTenantEvaluationResults(): Promise<EvaluationSummaryOut[]> {
  return request<EvaluationSummaryOut[]>('/tenant-admin/evaluation')
}

export function listTenantAuditEvents(): Promise<SecurityEventOut[]> {
  return request<SecurityEventOut[]>('/tenant-admin/audit')
}

// --- Reused as-is from the pre-existing recommendation-governance API
// (api/recommendation_governance.py) -- already tenant-scoped server-side,
// so no /tenant-admin/* equivalent exists for these two. ---

export function listTenantRecommendations(): Promise<RecommendationRecordOut[]> {
  return request<RecommendationRecordOut[]>('/recommendations')
}

export function getTenantRecommendationQualityMetrics(): Promise<RecommendationQualityMetricsOut> {
  return request<RecommendationQualityMetricsOut>('/recommendations/metrics')
}
