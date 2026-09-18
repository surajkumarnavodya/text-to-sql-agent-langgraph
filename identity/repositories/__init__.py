"""Data-access functions for the identity database, one module per concern
-- `users`, `sessions` (Phase A), `tokens`/`history`/`audit` (later phases).

Each function takes an already-open `sqlalchemy.orm.Session` (from
`identity.db.get_identity_session`) rather than opening its own -- callers
(`api/identity_auth.py` route handlers) own the session's lifetime via a
`with` block, the same "caller owns the connection" convention
`db/connection.py`'s functions already use for the business database.
Write operations commit internally (each repository call is one atomic
unit of work), matching this codebase's general preference for small,
independently-committed operations over long-lived multi-step
transactions.
"""

from __future__ import annotations
