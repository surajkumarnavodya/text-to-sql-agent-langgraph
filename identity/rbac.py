"""Granular, DB-backed permission layer for the new user/history/admin
endpoints -- additive to, and completely independent of, `agent.authz`'s
existing RBAC (which keeps gating every AI/RAG/SQL route exactly as it
already does; see `identity/models.py`'s module docstring for how the two
bridge via a shared base role vocabulary).

Pure policy, no FastAPI/database import (mirrors `agent/authz.py`'s own
"pure policy, separate from its web-framework wiring" split) -- the actual
per-request permission lookup (a DB join across `user_roles`/
`role_permissions`/`permissions`) lives in
`identity/repositories/users.py::get_user_permissions`, and the FastAPI
dependency wiring built on top of both lives in `api/identity_authz.py`.

Unlike `agent.authz.ROLE_PERMISSIONS` (a fixed, in-process Python dict), a
role's permission set here is genuinely DB-backed (`roles`/`permissions`/
`role_permissions` tables) so an operator can add a custom role or adjust
an existing one's grants without a code change -- `SEED_ROLES`/
`SEED_PERMISSIONS` below are only the *initial* data a fresh identity
database is populated with (`identity/bootstrap.py`), not a closed set
enforced afterward.
"""

from __future__ import annotations

from enum import Enum


class Permission(str, Enum):
    """One granular capability for the new user/history/admin surface.
    Deliberately dotted, resource-scoped names (`"users.read"`, not
    `"read_users"`) matching this feature's own specified vocabulary
    exactly.

    `*_OWN` vs. `*_ANY` is the recurring shape: a caller may always act on
    their *own* sessions/prompts/outputs/conversations/audit trail
    (enforced by an ownership check in the repository layer, not by this
    permission alone -- holding `SESSIONS_READ_OWN` never lets a caller
    pass someone *else's* user_id and get their sessions back), while the
    `_ANY` variants are what an admin/auditor/support role is actually
    granted to look across every account's data.
    """

    USERS_READ = "users.read"
    USERS_CREATE = "users.create"
    USERS_UPDATE = "users.update"
    USERS_DEACTIVATE = "users.deactivate"
    USERS_ASSIGN_ROLES = "users.assign_roles"
    SESSIONS_READ_OWN = "sessions.read_own"
    SESSIONS_REVOKE_OWN = "sessions.revoke_own"
    SESSIONS_REVOKE_ANY = "sessions.revoke_any"
    AUDIT_READ_OWN = "audit.read_own"
    AUDIT_READ_ANY = "audit.read_any"
    PROMPTS_READ_OWN = "prompts.read_own"
    PROMPTS_READ_ANY = "prompts.read_any"
    PROMPTS_DELETE_OWN = "prompts.delete_own"
    OUTPUTS_READ_OWN = "outputs.read_own"
    OUTPUTS_READ_ANY = "outputs.read_any"
    CONVERSATIONS_READ_OWN = "conversations.read_own"
    CONVERSATIONS_DELETE_OWN = "conversations.delete_own"
    SHARES_CREATE_OWN = "shares.create_own"
    SHARES_MANAGE_OWN = "shares.manage_own"
    SHARES_VIEW = "shares.view"
    AI_USE = "ai.use"
    RAG_QUERY = "rag.query"
    TEXT_TO_SQL_QUERY = "text_to_sql.query"
    ADMIN_DASHBOARD_READ = "admin.dashboard.read"
    ONBOARDING_MANAGE = "onboarding.manage"
    ONBOARDING_REVIEW = "onboarding.review"


#: Every permission's seed description, for the `permissions` table.
SEED_PERMISSIONS: tuple[tuple[Permission, str], ...] = (
    (Permission.USERS_READ, "View other users' accounts."),
    (Permission.USERS_CREATE, "Create new user accounts."),
    (Permission.USERS_UPDATE, "Edit another user's profile/status."),
    (Permission.USERS_DEACTIVATE, "Deactivate/suspend another user's account."),
    (Permission.USERS_ASSIGN_ROLES, "Assign or remove another user's roles."),
    (Permission.SESSIONS_READ_OWN, "View the caller's own active sessions."),
    (Permission.SESSIONS_REVOKE_OWN, "Revoke (sign out) the caller's own sessions."),
    (Permission.SESSIONS_REVOKE_ANY, "Revoke another user's sessions."),
    (Permission.AUDIT_READ_OWN, "View the caller's own audit history."),
    (Permission.AUDIT_READ_ANY, "View any user's audit history / system-wide audit logs."),
    (Permission.PROMPTS_READ_OWN, "View the caller's own prompt history."),
    (Permission.PROMPTS_READ_ANY, "View any user's prompt history."),
    (Permission.PROMPTS_DELETE_OWN, "Delete the caller's own prompts."),
    (Permission.OUTPUTS_READ_OWN, "View the caller's own AI output history."),
    (Permission.OUTPUTS_READ_ANY, "View any user's AI output history."),
    (Permission.CONVERSATIONS_READ_OWN, "View the caller's own conversations."),
    (Permission.CONVERSATIONS_DELETE_OWN, "Delete (soft-delete) the caller's own conversations."),
    (Permission.SHARES_CREATE_OWN, "Create a share for one of the caller's own conversations."),
    (
        Permission.SHARES_MANAGE_OWN,
        "Update settings, invite/remove members, regenerate the link, or revoke one of the "
        "caller's own shares.",
    ),
    (
        Permission.SHARES_VIEW,
        "View a conversation the caller has been given access to via sharing.",
    ),
    (Permission.AI_USE, "Use the AI assistant at all (chat/typed questions)."),
    (Permission.RAG_QUERY, "Query document/policy RAG sources."),
    (Permission.TEXT_TO_SQL_QUERY, "Use the text-to-SQL feature."),
    (Permission.ADMIN_DASHBOARD_READ, "View the admin dashboard / usage analytics."),
    (
        Permission.ONBOARDING_MANAGE,
        "Create, run, retry, cancel, and publish client-database onboarding jobs.",
    ),
    (
        Permission.ONBOARDING_REVIEW,
        "Decide (confirm/reject) an onboarding job's SME review items.",
    ),
)

_OWN_RESOURCE_PERMISSIONS: frozenset[Permission] = frozenset(
    {
        Permission.SESSIONS_READ_OWN,
        Permission.SESSIONS_REVOKE_OWN,
        Permission.AUDIT_READ_OWN,
        Permission.PROMPTS_READ_OWN,
        Permission.PROMPTS_DELETE_OWN,
        Permission.OUTPUTS_READ_OWN,
        Permission.CONVERSATIONS_READ_OWN,
        Permission.CONVERSATIONS_DELETE_OWN,
        Permission.SHARES_CREATE_OWN,
        Permission.SHARES_MANAGE_OWN,
    }
)

# Every account, regardless of AI-feature tier, can always manage its own
# profile/sessions/history -- that's an account-management concern, not an
# AI-capability tier. AI_USE/TEXT_TO_SQL_QUERY/RAG_QUERY below intentionally
# mirror agent.authz.ROLE_PERMISSIONS' own viewer<user<analyst progression
# for descriptive/analytics consistency -- the *actual* enforcement gate for
# those actions is still agent.authz.Permission.ASK/EXECUTE_SQL/
# POLICY_RAG_QUERY, completely unchanged; these codes exist so this
# feature's own admin dashboard/audit trail can report "what can this user
# do" from one consistent vocabulary, not as a second enforcement point for
# the same capability.
_VIEWER: frozenset[Permission] = _OWN_RESOURCE_PERMISSIONS | {
    Permission.AI_USE,
    # Granted broadly, not tied to a resource-ownership tier: whether a
    # caller may *view* a conversation shared with them depends entirely on
    # `identity.share_policy.authorize_share_action`'s own RBAC/ABAC checks
    # (share status, membership, expiry, tenant) -- this permission only
    # gates "is this account allowed to use the sharing feature's viewer
    # surface at all," the same way `AI_USE` gates the chat feature itself
    # without saying anything about which conversations a given call may
    # reach.
    Permission.SHARES_VIEW,
}
_USER: frozenset[Permission] = _VIEWER | {Permission.TEXT_TO_SQL_QUERY}
# ONBOARDING_REVIEW (SME sign-off on an inferred PII/relationship/semantic-
# label/golden-question claim) sits at the analyst tier -- a subject-matter
# reviewer needs domain judgment, not full user/account administration.
_ANALYST: frozenset[Permission] = _USER | {Permission.RAG_QUERY, Permission.ONBOARDING_REVIEW}
_ADMIN: frozenset[Permission] = _ANALYST | {
    Permission.USERS_READ,
    Permission.USERS_CREATE,
    Permission.USERS_UPDATE,
    Permission.USERS_DEACTIVATE,
    Permission.USERS_ASSIGN_ROLES,
    Permission.SESSIONS_REVOKE_ANY,
    Permission.AUDIT_READ_ANY,
    Permission.PROMPTS_READ_ANY,
    Permission.OUTPUTS_READ_ANY,
    Permission.ADMIN_DASHBOARD_READ,
    # ONBOARDING_MANAGE (creating a job and testing a live connection with
    # caller-supplied credentials, running discovery/profiling against a
    # real database, publishing) is an admin-tier action, not an analyst
    # one -- unlike ONBOARDING_REVIEW, it isn't a domain-judgment call.
    Permission.ONBOARDING_MANAGE,
}
# A read-focused compliance/oversight role -- can review any user's audit
# trail and the admin dashboard, but cannot create/edit/deactivate users or
# revoke sessions (a narrower slice of _ADMIN, not an extension of _USER).
_AUDITOR: frozenset[Permission] = _OWN_RESOURCE_PERMISSIONS | {
    Permission.AI_USE,
    Permission.AUDIT_READ_ANY,
    Permission.ADMIN_DASHBOARD_READ,
}
# Can see and act on user accounts/sessions day-to-day, without the full
# provisioning power (create/deactivate/assign_roles) _ADMIN has.
_MANAGER: frozenset[Permission] = _ANALYST | {
    Permission.USERS_READ,
    Permission.SESSIONS_REVOKE_ANY,
    Permission.AUDIT_READ_ANY,
    Permission.ADMIN_DASHBOARD_READ,
}
# A narrow helpdesk role: look up an account and sign it out everywhere (a
# common support request), nothing else administrative.
_SUPPORT: frozenset[Permission] = _USER | {
    Permission.USERS_READ,
    Permission.SESSIONS_REVOKE_ANY,
}

#: Seed data for a fresh identity database (`identity/bootstrap.py`):
#: (name, description, is_system_role, granted permissions). The first
#: four names are deliberately identical to `agent.authz.ROLE_PERMISSIONS`'
#: own keys -- see this module's own docstring for why that bridge matters.
SEED_ROLES: tuple[tuple[str, str, bool, frozenset[Permission]], ...] = (
    ("viewer", "Read-only access to AI features and their own account.", True, _VIEWER),
    ("user", "Standard account -- full self-service use of AI features.", True, _USER),
    ("analyst", "Adds document/policy RAG access on top of 'user'.", True, _ANALYST),
    ("admin", "Full account/session/user management and audit visibility.", True, _ADMIN),
    ("auditor", "Read-only compliance/audit oversight across all users.", True, _AUDITOR),
    ("manager", "Day-to-day user/session oversight without full provisioning.", True, _MANAGER),
    ("support", "Helpdesk: look up accounts and revoke their sessions.", True, _SUPPORT),
)

#: The 4 role names `agent.authz.ROLE_PERMISSIONS` already knows -- a
#: locally-authenticated user's roles (`AuthIdentity.roles`) should always
#: include at least one of these for existing AI/RAG/SQL routes to grant
#: any access at all. `auditor`/`manager`/`support` are additive extras
#: this feature introduces; assigning *only* one of those three to a user
#: (with none of the four base roles) is a valid, if unusual, configuration
#: that would leave `agent.authz.permissions_for` granting nothing on the
#: AI-feature side -- a deliberate, fail-closed consequence of
#: `agent/authz.py`'s own "an unrecognized role name grants no
#: permissions" contract, not a bug in this bridge.
BASE_ROLE_NAMES: frozenset[str] = frozenset({"viewer", "user", "analyst", "admin"})

#: Default role newly-registered/admin-created users are assigned, absent
#: any other instruction.
DEFAULT_ROLE_NAME = "user"


def permissions_for_roles(
    role_permission_map: dict[str, frozenset[Permission]], roles: tuple[str, ...]
) -> frozenset[Permission]:
    """Union of every permission granted by any of `roles`, given an
    already-fetched `{role_name: granted_permissions}` map (typically
    built once per request by `identity.repositories.users
    .get_user_permissions`, since the real mapping is DB-backed, not this
    module's own `SEED_ROLES` -- that's only ever the initial seed).

    An unrecognized role name contributes no permissions -- fail closed,
    mirroring `agent.authz.permissions_for`'s identical contract.
    """
    result: set[Permission] = set()
    for role in roles:
        result |= role_permission_map.get(role, frozenset())
    return frozenset(result)
