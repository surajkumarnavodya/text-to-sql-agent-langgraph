"""Self-hosted user accounts, RBAC, sessions, and AI activity history --
backed by a dedicated PostgreSQL database (`Settings.auth_database_url`),
never the business/HR database(s) the text-to-SQL agent queries
(`Settings.databases`).

Off by default (`Settings.local_auth_enabled`). See this package's own
modules for the pieces:

- `identity/models.py` -- SQLAlchemy 2.0 declarative ORM models (the one
  genuinely new pattern this introduces into this codebase -- every other
  DB-backed module here, `db/`, `rag/store.py`, `moderation/store.py`, uses
  raw SQLAlchemy Core with hand-written idempotent `ensure_schema()`
  functions instead of an ORM/migrations. This package is real-migration-
  managed (`identity/migrations/`, Alembic) specifically because it's a
  greenfield database with genuine "roll this forward, roll this back"
  schema-evolution needs, unlike the small, stable, hand-maintained schemas
  the rest of this app owns.
- `identity/db.py` -- the pooled engine + session factory.
- `identity/security.py` -- password hashing (Argon2id) and locally-issued
  JWT access tokens / opaque refresh tokens.
- `identity/rbac.py` -- the granular permission layer for the new user/
  history/admin endpoints, additive to (never a replacement for)
  `agent.authz`'s existing RBAC, which keeps gating every AI/RAG/SQL route
  exactly as it already does.
- `identity/repositories/` -- data-access functions, one module per
  concern (users, sessions, tokens, history, audit).
- `identity/bootstrap.py` -- safe first-admin-account creation.

See `docs/AUTH_USER_MANAGEMENT.md` for the full architecture and security
model, and `CLAUDE.md`'s "Authentication, authorization, and the 2026
security hardening passes" section for how this relates to the pre-existing
OIDC/RBAC system.
"""

from __future__ import annotations
