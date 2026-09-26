"""Phase 0 load-test seeding: RBAC roles/permissions + N test accounts,
via the *real* creation path (`identity.repositories.users.create_user` --
real Argon2id hashing, no policy bypass), exactly the way
`scripts/bootstrap_admin.py` seeds RBAC for a real deployment. See
`docs/SCALE_OUT_PROMPT.md`'s Phase 0 section.

Usage (from repo root, or inside the `api` container against the seeded
load-test Postgres -- see docker-compose.loadtest.yml):

    python -m eval.load.seed_users

Requires `LOCAL_AUTH_ENABLED=true` and `AUTH_DATABASE_URL` already set in
the environment (same requirement as `scripts/bootstrap_admin.py`), and
the identity schema already migrated (`alembic -c identity/alembic.ini
upgrade head` -- `make load-test` runs that first). Idempotent: re-running
against already-seeded users is a safe no-op (`create_user` raises
`DuplicateUserError`, caught and skipped here) -- so `make load-test` can
call this on every run without accumulating duplicate accounts.

Deliberately NOT `scripts/bootstrap_admin.py` itself: that script creates
exactly one (optional) admin account. This creates `LOAD_TEST_USER_COUNT`
plain "user"-role accounts with a shared, well-known password
(`LOAD_TEST_USER_PASSWORD`) so `eval/load/k6/lib/auth.js` can log in as
any of them without a secrets file -- acceptable only because this
database is throwaway, load-test-only infrastructure, never a real
deployment's identity store.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from identity.bootstrap import seed_rbac  # noqa: E402
from identity.db import get_identity_session  # noqa: E402
from identity.exceptions import DuplicateUserError, IdentityError  # noqa: E402
from identity.repositories.users import create_user  # noqa: E402

from config.settings import ConfigurationError, get_settings  # noqa: E402

USER_COUNT = int(os.environ.get("LOAD_TEST_USER_COUNT", "50"))
PASSWORD = os.environ.get("LOAD_TEST_USER_PASSWORD", "LoadTest!Passw0rd123")
EMAIL_DOMAIN = os.environ.get("LOAD_TEST_USER_EMAIL_DOMAIN", "loadtest.example.internal")


def main() -> int:
    try:
        settings = get_settings()
    except ConfigurationError as exc:
        print(f"FAIL: {exc}")
        return 1

    if not settings.local_auth_enabled:
        print("LOCAL_AUTH_ENABLED is not true -- nothing to seed. Set it in .env.loadtest.")
        return 1

    try:
        session = get_identity_session(settings)
    except IdentityError as exc:
        print(f"FAIL: {exc.safe_message}")
        return 1

    seed_rbac(session)
    print("PASS: RBAC roles/permissions are up to date.")

    created = 0
    for i in range(USER_COUNT):
        email = f"loadtest-user-{i:04d}@{EMAIL_DOMAIN}"
        try:
            create_user(
                session,
                email=email,
                password=PASSWORD,
                display_name=f"Load Test User {i:04d}",
                status="active",
            )
            created += 1
        except DuplicateUserError:
            pass  # Already seeded by an earlier `make load-test` run -- fine.

    print(f"PASS: {created} new user(s) created, {USER_COUNT - created} already existed.")
    print(
        f"      Login as loadtest-user-0000..{USER_COUNT - 1:04d}@{EMAIL_DOMAIN} / <LOAD_TEST_USER_PASSWORD>"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
