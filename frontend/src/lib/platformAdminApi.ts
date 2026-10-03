import { request } from './api'
import type {
  AssignRoleRequest,
  AuditLogOut,
  ConfigStatusOut,
  CreateTenantRequest,
  DatabaseStatusOut,
  PerformanceMetricsResponse,
  PlatformOnboardingJobOut,
  PlatformUserOut,
  RoleOut,
  SecurityEventOut,
  SemanticReviewQueueOut,
  SetTenantStatusRequest,
  TenantOut,
} from './types'

/** Thin wrappers around `/platform-admin/*` (`api/platform_admin.py`,
 * Prompt 28) -- mirrors `lib/onboardingApi.ts`/`lib/semanticCatalogApi.ts`'s
 * own established convention exactly: every function is a bare
 * `request()` call with no extra logic of its own. Every one of these
 * requires a local account holding `identity.rbac
 * .Permission.PLATFORM_ADMIN` -- `request()`'s existing bearer-token
 * handling already covers authentication; the server returns 403 for
 * anyone else, including a tenant-scoped `admin`. */

export function listTenants(): Promise<TenantOut[]> {
  return request<TenantOut[]>('/platform-admin/tenants')
}

export function createTenant(payload: CreateTenantRequest): Promise<TenantOut> {
  return request<TenantOut>('/platform-admin/tenants', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function setTenantStatus(
  tenantId: string,
  payload: SetTenantStatusRequest,
): Promise<TenantOut> {
  return request<TenantOut>(`/platform-admin/tenants/${tenantId}/status`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function listPlatformUsers(params?: {
  tenantId?: string
  roleName?: string
  status?: string
}): Promise<PlatformUserOut[]> {
  const query = new URLSearchParams()
  if (params?.tenantId) query.set('tenant_id', params.tenantId)
  if (params?.roleName) query.set('role_name', params.roleName)
  if (params?.status) query.set('status', params.status)
  const suffix = query.toString() ? `?${query.toString()}` : ''
  return request<PlatformUserOut[]>(`/platform-admin/users${suffix}`)
}

export function assignUserRole(
  userId: string,
  payload: AssignRoleRequest,
): Promise<PlatformUserOut> {
  return request<PlatformUserOut>(`/platform-admin/users/${userId}/roles`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function removeUserRole(userId: string, roleName: string): Promise<PlatformUserOut> {
  return request<PlatformUserOut>(`/platform-admin/users/${userId}/roles/${roleName}`, {
    method: 'DELETE',
  })
}

export function listRoles(): Promise<RoleOut[]> {
  return request<RoleOut[]>('/platform-admin/roles')
}

export function listPlatformDatabases(): Promise<DatabaseStatusOut[]> {
  return request<DatabaseStatusOut[]>('/platform-admin/databases')
}

export function getSemanticReviewQueue(): Promise<SemanticReviewQueueOut> {
  return request<SemanticReviewQueueOut>('/platform-admin/semantic-review-queue')
}

export function listPlatformJobs(status?: string): Promise<PlatformOnboardingJobOut[]> {
  const suffix = status ? `?status=${encodeURIComponent(status)}` : ''
  return request<PlatformOnboardingJobOut[]>(`/platform-admin/jobs${suffix}`)
}

export function getPlatformMetrics(): Promise<PerformanceMetricsResponse> {
  return request<PerformanceMetricsResponse>('/platform-admin/metrics')
}

export function listSecurityEvents(params?: {
  limit?: number
  severity?: string
  eventType?: string
}): Promise<SecurityEventOut[]> {
  const query = new URLSearchParams()
  if (params?.limit) query.set('limit', String(params.limit))
  if (params?.severity) query.set('severity', params.severity)
  if (params?.eventType) query.set('event_type', params.eventType)
  const suffix = query.toString() ? `?${query.toString()}` : ''
  return request<SecurityEventOut[]>(`/platform-admin/security-events${suffix}`)
}

export function listAuditLogs(params?: {
  action?: string
  resourceType?: string
  outcome?: string
}): Promise<AuditLogOut[]> {
  const query = new URLSearchParams()
  if (params?.action) query.set('action', params.action)
  if (params?.resourceType) query.set('resource_type', params.resourceType)
  if (params?.outcome) query.set('outcome', params.outcome)
  const suffix = query.toString() ? `?${query.toString()}` : ''
  return request<AuditLogOut[]>(`/platform-admin/audit-logs${suffix}`)
}

export function getConfigStatus(): Promise<ConfigStatusOut> {
  return request<ConfigStatusOut>('/platform-admin/config-status')
}
