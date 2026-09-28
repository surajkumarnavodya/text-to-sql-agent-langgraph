import { request } from './api'
import type {
  AuthSessionOut,
  ConversationListResponse,
  GoogleNonceResponse,
  LinkedIdentityListResponse,
  LocalUser,
  MessageListResponse,
  MessageResponse,
  SearchResponse,
  ServerConversation,
  TokenResponse,
} from './types'

/** Thin wrappers around `POST /auth/*` (`api/identity_auth.py`) -- this
 * app's own self-hosted accounts, distinct from `lib/auth.ts`'s OIDC flow.
 * Reuses `lib/api.ts`'s `request()` for the same fetch/error-handling
 * logic every other endpoint in this app already goes through; the
 * refresh-token cookie itself is never touched here (or anywhere in
 * frontend code) -- it's `HttpOnly`, set/read by the browser automatically
 * on same-origin requests to `/auth/*`, never visible to JavaScript. See
 * `identity/security.py`'s own module docstring for why.
 */

export function registerUser(payload: {
  email: string
  password: string
  display_name?: string
}): Promise<TokenResponse | MessageResponse> {
  return request('/auth/register', { method: 'POST', body: JSON.stringify(payload) })
}

export function loginUser(email: string, password: string): Promise<TokenResponse> {
  return request<TokenResponse>('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ email, password }),
  })
}

// --- Google sign-in (2026-09-28, security/google_oidc.py) -- the ID token
// (`credential`) comes from Google Identity Services' JS callback, never
// decoded/trusted client-side; the backend does all real verification. ---

/** A short-lived, single-use nonce to pass into `google.accounts.id
 * .initialize({nonce, ...})` before rendering the Sign In With Google
 * button -- see `security/google_oidc.py`'s own module docstring for
 * exactly what this does and does not protect against. */
export function fetchGoogleSigninNonce(): Promise<GoogleNonceResponse> {
  return request<GoogleNonceResponse>('/auth/google/nonce')
}

/** Sign in, or sign up on first use of a given Google identity -- one
 * backend flow decides which, the caller never has to say. */
export function googleSignIn(credential: string): Promise<TokenResponse> {
  return request<TokenResponse>('/auth/google', {
    method: 'POST',
    body: JSON.stringify({ credential }),
  })
}

/** The authenticated caller's own linked external identities, for an
 * account-settings "connected accounts" view. */
export function listLinkedIdentities(): Promise<LinkedIdentityListResponse> {
  return request<LinkedIdentityListResponse>('/auth/google/link')
}

/** Links a verified Google identity to the *currently signed-in* local
 * account -- the safe, explicit alternative to auto-merging by email (see
 * `api/identity_auth.py::google_signin`'s own docstring). */
export function linkGoogleAccount(credential: string): Promise<LinkedIdentityListResponse> {
  return request<LinkedIdentityListResponse>('/auth/google/link', {
    method: 'POST',
    body: JSON.stringify({ credential }),
  })
}

/** Removes the caller's own Google link -- rejected server-side (409) if
 * this is the account's only usable sign-in method. */
export function unlinkGoogleAccount(): Promise<LinkedIdentityListResponse> {
  return request<LinkedIdentityListResponse>('/auth/google/link', { method: 'DELETE' })
}

/** Reads the refresh-token cookie automatically (same-origin request) --
 * never called with a body. Rejects (throws `ApiError`, status 401 or 404)
 * if there's no live session to refresh, or local auth isn't enabled. */
export function refreshAccessToken(): Promise<TokenResponse> {
  return request<TokenResponse>('/auth/refresh', { method: 'POST' })
}

export function logoutUser(): Promise<MessageResponse> {
  return request<MessageResponse>('/auth/logout', { method: 'POST' })
}

export function logoutAllSessions(): Promise<MessageResponse> {
  return request<MessageResponse>('/auth/logout-all', { method: 'POST' })
}

export function getCurrentUser(): Promise<LocalUser> {
  return request<LocalUser>('/auth/me')
}

export function changePassword(
  currentPassword: string,
  newPassword: string,
): Promise<MessageResponse> {
  return request<MessageResponse>('/auth/change-password', {
    method: 'POST',
    body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
  })
}

export function forgotPassword(email: string): Promise<MessageResponse> {
  return request<MessageResponse>('/auth/forgot-password', {
    method: 'POST',
    body: JSON.stringify({ email }),
  })
}

export function resetPassword(token: string, newPassword: string): Promise<MessageResponse> {
  return request<MessageResponse>('/auth/reset-password', {
    method: 'POST',
    body: JSON.stringify({ token, new_password: newPassword }),
  })
}

export function verifyEmail(token: string): Promise<MessageResponse> {
  return request<MessageResponse>('/auth/verify-email', {
    method: 'POST',
    body: JSON.stringify({ token }),
  })
}

export function resendVerification(email: string): Promise<MessageResponse> {
  return request<MessageResponse>('/auth/resend-verification', {
    method: 'POST',
    body: JSON.stringify({ email }),
  })
}

export function listSessions(): Promise<{ sessions: AuthSessionOut[] }> {
  return request('/auth/sessions')
}

export function revokeSession(sessionId: string): Promise<MessageResponse> {
  return request<MessageResponse>(`/auth/sessions/${sessionId}`, { method: 'DELETE' })
}

/** True only if the response is a `TokenResponse` (immediate sign-in) --
 * `POST /auth/register` returns a `MessageResponse` instead when
 * `REQUIRE_EMAIL_VERIFICATION` is on, since the account can't sign in yet. */
export function isTokenResponse(
  body: TokenResponse | MessageResponse,
): body is TokenResponse {
  return 'access_token' in body
}

/** Sets/changes the caller's own display name -- `PATCH /auth/me`, the
 * profile-completion path for `LocalUser.needs_profile_completion`. */
export function updateProfile(displayName: string): Promise<LocalUser> {
  return request<LocalUser>('/auth/me', {
    method: 'PATCH',
    body: JSON.stringify({ display_name: displayName }),
  })
}

// --- Server-side chat history (api/chat_history.py) -- see
// frontend/src/lib/types.ts's own docstring for what this is and isn't. ---

export function listConversations(
  params: { limit?: number; offset?: number; includeArchived?: boolean } = {},
): Promise<ConversationListResponse> {
  const query = new URLSearchParams()
  if (params.limit) query.set('limit', String(params.limit))
  if (params.offset) query.set('offset', String(params.offset))
  if (params.includeArchived) query.set('include_archived', 'true')
  const suffix = query.toString() ? `?${query.toString()}` : ''
  return request<ConversationListResponse>(`/conversations${suffix}`)
}

export function createConversation(title?: string): Promise<ServerConversation> {
  return request<ServerConversation>('/conversations', {
    method: 'POST',
    body: JSON.stringify({ title: title ?? null }),
  })
}

export function getConversation(conversationId: string): Promise<ServerConversation> {
  return request<ServerConversation>(`/conversations/${conversationId}`)
}

export function renameConversationOnServer(
  conversationId: string,
  title: string,
): Promise<ServerConversation> {
  return request<ServerConversation>(`/conversations/${conversationId}`, {
    method: 'PATCH',
    body: JSON.stringify({ title }),
  })
}

export function archiveConversation(
  conversationId: string,
  archived: boolean,
): Promise<ServerConversation> {
  return request<ServerConversation>(`/conversations/${conversationId}`, {
    method: 'PATCH',
    body: JSON.stringify({ archived }),
  })
}

/** Soft-deletes on the server -- see `identity.repositories.history
 * .soft_delete_conversation`'s own docstring. Only ever called from an
 * explicit, user-confirmed delete action -- never from logout, session
 * expiry, or a browser/device switch (see
 * docs/chat-history-architecture.md's retention section). */
export function deleteConversationOnServer(conversationId: string): Promise<MessageResponse> {
  return request<MessageResponse>(`/conversations/${conversationId}`, { method: 'DELETE' })
}

export function listMessages(
  conversationId: string,
  params: { limit?: number; offset?: number } = {},
): Promise<MessageListResponse> {
  const query = new URLSearchParams()
  if (params.limit) query.set('limit', String(params.limit))
  if (params.offset) query.set('offset', String(params.offset))
  const suffix = query.toString() ? `?${query.toString()}` : ''
  return request<MessageListResponse>(`/conversations/${conversationId}/messages${suffix}`)
}

/** Searches the authenticated user's *complete* server-side chat history --
 * not just whatever conversations happen to be loaded in the frontend
 * already (see `identity.repositories.history.search_history`'s own
 * docstring). */
export function searchChatHistory(
  query: string,
  params: { limit?: number; offset?: number } = {},
): Promise<SearchResponse> {
  const search = new URLSearchParams({ q: query })
  if (params.limit) search.set('limit', String(params.limit))
  if (params.offset) search.set('offset', String(params.offset))
  return request<SearchResponse>(`/chat/search?${search.toString()}`)
}
