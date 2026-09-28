import { getBearerToken } from '@/store/authStore'
import { request } from './api'
import type {
  AcceptInvitationResponse,
  CreateShareRequest,
  InviteMemberRequest,
  InviteMemberResponse,
  Share,
  ShareResponse,
  SharedConversation,
  UpdateShareRequest,
} from './types'

/** Thin wrappers around `api/shares.py` -- reuses `lib/api.ts`'s `request()`
 * for the same fetch/error-handling/auth-header logic every other endpoint
 * in this app already goes through.
 *
 * `getSharedConversation`/`getSharedAttachmentBlobUrl` are the only two
 * functions here that work with **no signed-in session at all** ("anyone
 * with the link" mode) -- `request()` already omits the `Authorization`
 * header when there's no bearer token to send, so no special handling is
 * needed here; if the caller *does* have a live local session, the same
 * token is attached automatically, which is what lets an authenticated
 * invited member open the identical URL and be recognized as a member
 * rather than an anonymous visitor. */

export function createShare(
  conversationId: string,
  payload: CreateShareRequest = {},
): Promise<ShareResponse> {
  return request<ShareResponse>(`/conversations/${conversationId}/share`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function getShare(conversationId: string): Promise<Share> {
  return request<Share>(`/conversations/${conversationId}/share`)
}

export function updateShare(conversationId: string, payload: UpdateShareRequest): Promise<Share> {
  return request<Share>(`/conversations/${conversationId}/share`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  })
}

export function revokeShare(conversationId: string, version: number): Promise<Share> {
  return request<Share>(`/conversations/${conversationId}/share/revoke`, {
    method: 'POST',
    body: JSON.stringify({ version }),
  })
}

export function regenerateShareLink(conversationId: string): Promise<ShareResponse> {
  return request<ShareResponse>(`/conversations/${conversationId}/share/link/regenerate`, {
    method: 'POST',
  })
}

export function inviteShareMember(
  conversationId: string,
  payload: InviteMemberRequest,
): Promise<InviteMemberResponse> {
  return request<InviteMemberResponse>(`/conversations/${conversationId}/share/members/invite`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function removeShareMember(conversationId: string, memberId: string): Promise<Share> {
  return request<Share>(`/conversations/${conversationId}/share/members/${memberId}`, {
    method: 'DELETE',
  })
}

export function acceptShareInvitation(opaqueToken: string): Promise<AcceptInvitationResponse> {
  return request<AcceptInvitationResponse>(`/share-invitations/${opaqueToken}/accept`, {
    method: 'POST',
  })
}

/** `ref` is either a share id (an authenticated member/owner's stable,
 * bookmarkable path) or an opaque link token (the "anyone with the link"
 * bearer path) -- the backend disambiguates by shape; this function never
 * needs to know which one it has. */
export function getSharedConversation(ref: string): Promise<SharedConversation> {
  return request<SharedConversation>(`/share-view/${encodeURIComponent(ref)}`)
}

/** Fetches one shared attachment's bytes and returns a blob object URL --
 * same reasoning as `lib/api.ts::fetchMediaBlobUrl`: this route needs
 * whatever `Authorization` header (if any) the caller has, which a plain
 * `<img src="...">` couldn't send, and re-checks full share authorization
 * server-side on every call regardless of what a prior view call returned.
 * Callers must `URL.revokeObjectURL` the result once it's no longer shown. */
export async function getSharedAttachmentBlobUrl(
  ref: string,
  attachmentId: string,
): Promise<string> {
  const headers = new Headers()
  const token = getBearerToken()
  if (token) headers.set('Authorization', `Bearer ${token}`)
  const response = await fetch(
    `/share-view/${encodeURIComponent(ref)}/attachments/${encodeURIComponent(attachmentId)}`,
    { headers },
  )
  if (!response.ok) {
    throw new Error('This attachment is not available.')
  }
  const blob = await response.blob()
  return URL.createObjectURL(blob)
}
