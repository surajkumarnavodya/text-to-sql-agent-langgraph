import { request } from './api'
import type {
  CreateOnboardingJobRequest,
  DecideReviewItemRequest,
  OnboardingArtifact,
  OnboardingJob,
  OnboardingReviewItem,
  PublishJobRequest,
  RunDiscoveryRequest,
} from './types'

/** Thin wrappers around `POST/GET /onboarding/*` (`api/onboarding.py`,
 * Prompt 08/26) -- the client-database onboarding engine. Reuses
 * `lib/api.ts`'s `request()` for the same fetch/auth/error-handling
 * logic every other endpoint in this app already goes through, the
 * identical convention `lib/identityApi.ts` already established for its
 * own cohesive domain.
 *
 * Every one of these requires a local account (`Depends(require_local_user)`
 * server-side) -- `request()`'s existing bearer-token handling already
 * covers this with no special case here. `db_password` is accepted on
 * three of the request types below and is never echoed back anywhere --
 * `OnboardingJob` has no password field to display even if a caller tried. */

export function createOnboardingJob(payload: CreateOnboardingJobRequest): Promise<OnboardingJob> {
  return request<OnboardingJob>('/onboarding/jobs', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function listOnboardingJobs(): Promise<OnboardingJob[]> {
  return request<OnboardingJob[]>('/onboarding/jobs')
}

export function getOnboardingJob(jobId: string): Promise<OnboardingJob> {
  return request<OnboardingJob>(`/onboarding/jobs/${jobId}`)
}

export function runOnboardingDiscovery(
  jobId: string,
  payload: RunDiscoveryRequest,
): Promise<OnboardingJob> {
  return request<OnboardingJob>(`/onboarding/jobs/${jobId}/discover`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function listOnboardingReviewItems(jobId: string): Promise<OnboardingReviewItem[]> {
  return request<OnboardingReviewItem[]>(`/onboarding/jobs/${jobId}/review-items`)
}

export function decideOnboardingReviewItem(
  jobId: string,
  itemId: string,
  payload: DecideReviewItemRequest,
): Promise<OnboardingReviewItem> {
  return request<OnboardingReviewItem>(
    `/onboarding/jobs/${jobId}/review-items/${itemId}/decide`,
    { method: 'POST', body: JSON.stringify(payload) },
  )
}

export function publishOnboardingJob(
  jobId: string,
  payload: PublishJobRequest,
): Promise<OnboardingJob> {
  return request<OnboardingJob>(`/onboarding/jobs/${jobId}/publish`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function cancelOnboardingJob(jobId: string): Promise<OnboardingJob> {
  return request<OnboardingJob>(`/onboarding/jobs/${jobId}/cancel`, { method: 'POST' })
}

export function retryOnboardingJob(jobId: string): Promise<OnboardingJob> {
  return request<OnboardingJob>(`/onboarding/jobs/${jobId}/retry`, { method: 'POST' })
}

export function listOnboardingArtifacts(jobId: string): Promise<OnboardingArtifact[]> {
  return request<OnboardingArtifact[]>(`/onboarding/jobs/${jobId}/artifacts`)
}
