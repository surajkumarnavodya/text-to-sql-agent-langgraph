"""Outbound "email" for password-reset/email-verification links.

**A disclosed placeholder, not a real mail integration** -- this project
has no SMTP/SES/SendGrid credentials or dependency anywhere, and building
one was out of scope for this pass (see `Settings.require_email_verification`'s
own docstring: it defaults off specifically because of this gap). The one
implemented "provider" logs the would-be email to a dedicated logger
(`identity.email`, distinct from `security.audit_log`'s event stream, which
deliberately never carries content) -- the same "console backend" idea
every major web framework ships for local development, not a claim that
this delivers real mail. A real deployment wanting actual email delivery
needs to replace `send_password_reset_email`/`send_verification_email`
below with a real provider call; nothing else in `identity/` needs to
change to support that (both call sites already pass only recipient +
token + base URL, no framework-specific detail).
"""

from __future__ import annotations

import logging

logger = logging.getLogger("identity.email")


def send_password_reset_email(to_email: str, raw_token: str, *, base_url: str) -> None:
    logger.info(
        "[identity.email] Password reset requested for %s -- link: %s/reset-password?token=%s",
        to_email,
        base_url.rstrip("/"),
        raw_token,
    )


def send_verification_email(to_email: str, raw_token: str, *, base_url: str) -> None:
    logger.info(
        "[identity.email] Verify your email for %s -- link: %s/verify-email?token=%s",
        to_email,
        base_url.rstrip("/"),
        raw_token,
    )
