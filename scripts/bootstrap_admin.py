"""Standalone entry point: seed the identity database's RBAC data and
(optionally) create the first admin account.

Usage (from repo root, with the venv activated):

    python scripts\\bootstrap_admin.py

Requires `LOCAL_AUTH_ENABLED=true` and `AUTH_DATABASE_URL` set in `.env`
(the identity database itself -- see `.env.example`'s identity/auth
section). Always seeds `identity.rbac.SEED_ROLES`/`SEED_PERMISSIONS`
(idempotent -- safe to re-run). Only creates the bootstrap admin account
if `BOOTSTRAP_ADMIN_ENABLED=true` **and** `BOOTSTRAP_ADMIN_EMAIL`/
`BOOTSTRAP_ADMIN_PASSWORD` are both set; also idempotent (re-running this
after the admin account already exists is a safe no-op, never resets its
password -- see `identity.bootstrap.bootstrap_admin_user`'s own docstring).

Never creates database tables -- run `alembic -c identity/alembic.ini
upgrade head` first (see `docs/AUTH_USER_MANAGEMENT.md`'s local-setup
section).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from identity.bootstrap import bootstrap_admin_user, seed_rbac  # noqa: E402
from identity.db import get_identity_session  # noqa: E402
from identity.exceptions import IdentityError  # noqa: E402

from config.settings import ConfigurationError, get_settings  # noqa: E402


def main() -> int:
    settings = get_settings()
    if not settings.local_auth_enabled:
        print(
            "LOCAL_AUTH_ENABLED is not true in .env -- nothing to bootstrap. "
            "Set LOCAL_AUTH_ENABLED=true and AUTH_DATABASE_URL first."
        )
        return 1

    try:
        session = get_identity_session(settings)
    except IdentityError as exc:
        print(f"FAIL: {exc.safe_message}")
        return 1

    try:
        print("Seeding RBAC roles/permissions...")
        seed_rbac(session)
        print("PASS: RBAC roles/permissions are up to date.")

        if not settings.bootstrap_admin_enabled:
            print(
                "BOOTSTRAP_ADMIN_ENABLED is not true -- skipping admin account creation. "
                "Set BOOTSTRAP_ADMIN_ENABLED=true plus BOOTSTRAP_ADMIN_EMAIL/"
                "BOOTSTRAP_ADMIN_PASSWORD in .env if you want one created."
            )
            return 0

        user = bootstrap_admin_user(session, settings)
        if user is not None:
            print(f"PASS: admin account ready: {user.email}")
        return 0
    except (ConfigurationError, ValueError) as exc:
        print(f"FAIL: {exc}")
        return 1
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
