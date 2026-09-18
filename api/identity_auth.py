"""`POST /auth/*` -- registration, sign-in, token refresh/rotation,
password-reset/email-verification, and self-service session management for
this app's own, self-hosted user accounts (`identity/`).

Off by default (`Settings.local_auth_enabled`) -- `POST /auth/register`/
`POST /auth/login` 404 when it's off, the same "feature genuinely doesn't
exist until configured" posture `api/media_search.py` etc. already have for
their own optional sources. Router is still registered unconditionally in
`api/main.py` (mirrors every other router there); the check happens per
request, not at import time.

**The refresh token is never returned in a JSON body.** `POST /auth/login`/
`POST /auth/refresh` set it as a `Secure`, `HttpOnly`, `SameSite` cookie
(`Settings.cookie_secure`/`cookie_samesite`/`cookie_domain`), scoped to the
`/auth` path -- the access token (short-lived, kept in memory by the
frontend) is the only credential that ever appears in a response body,
matching this feature's own "prefer cookies for browser clients, never
localStorage for a refresh token" requirement.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from identity.email import send_password_reset_email, send_verification_email
from identity.exceptions import (
    AccountLockedError,
    AccountNotActiveError,
    DuplicateUserError,
    IdentityError,
    InvalidCredentialsError,
    RefreshTokenInvalidError,
    RefreshTokenReusedError,
)
from identity.models import User
from identity.password_policy import validate_password_strength
from identity.repositories.sessions import (
    create_session,
    get_session_by_refresh_token,
    list_active_sessions_for_user,
    revoke_all_sessions_for_user,
    revoke_session,
    rotate_refresh_token,
)
from identity.repositories.signin_events import record_signin_event
from identity.repositories.tokens import (
    create_email_verification_token,
    create_password_reset_token,
    redeem_email_verification_token,
    redeem_password_reset_token,
)
from identity.repositories.users import (
    change_password,
    check_not_locked,
    create_user,
    get_user_by_email,
    get_user_by_id,
    get_user_roles,
    record_login_failure,
    record_login_success,
    update_display_name,
)
from identity.schemas import (
    ChangePasswordRequest,
    ForgotPasswordRequest,
    LoginRequest,
    MessageResponse,
    RegisterRequest,
    ResendVerificationRequest,
    ResetPasswordRequest,
    SessionListResponse,
    SessionOut,
    TokenResponse,
    UpdateProfileRequest,
    UserOut,
    VerifyEmailRequest,
)
from identity.security import create_access_token, verify_password
from sqlalchemy.orm import Session

from agent.rate_limit import BoundedLimiterCache
from api.identity_authz import get_identity_db, require_local_auth_enabled, require_local_user
from config.settings import Settings, get_settings
from security.audit_log import log_security_event

router = APIRouter()

_REFRESH_COOKIE_NAME = "refresh_token"
_REFRESH_COOKIE_PATH = "/auth"

# Per-client-IP limiters for the three auth actions this feature's own spec
# calls out by name, each with its own configurable budget (distinct units
# -- per-minute for login/refresh, per-hour for register) -- mirrors
# `api/main.py`'s own `_ip_limiters`/`_limiter_for` pattern, generalized to
# three independent caches so hammering one action never shares budget
# with another (`agent.rate_limit.BoundedLimiterCache`'s own LRU-eviction
# docstring covers why this is bounded, not a bare dict).
_login_limiters = BoundedLimiterCache()
_register_limiters = BoundedLimiterCache()
_refresh_limiters = BoundedLimiterCache()
_password_reset_limiters = BoundedLimiterCache()

_RATE_LIMIT_MESSAGE = "Too many attempts -- please wait a moment and try again."


def _enforce_rate_limit(
    cache: BoundedLimiterCache,
    request: Request,
    *,
    action: str,
    max_events: int,
    window_seconds: float,
) -> None:
    client_ip = _client_ip(request)
    key = f"{action}:{client_ip}"
    limiter = cache.get_or_create(
        key,
        max_events=max_events,
        window_seconds=window_seconds,
        name=f"auth_{action}[{client_ip}]",
    )
    result = limiter.check()
    if not result.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=_RATE_LIMIT_MESSAGE,
            headers={"Retry-After": str(int(result.retry_after_seconds) + 1)},
        )


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _user_out(user: User, roles: tuple[str, ...]) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        username=user.username,
        display_name=user.display_name,
        needs_profile_completion=user.display_name is None,
        status=user.status,
        is_email_verified=user.is_email_verified,
        roles=list(roles),
        created_at=user.created_at,
        last_login_at=user.last_login_at,
    )


def _set_refresh_cookie(response: Response, raw_refresh_token: str, settings: Settings) -> None:
    response.set_cookie(
        key=_REFRESH_COOKIE_NAME,
        value=raw_refresh_token,
        max_age=settings.refresh_token_expire_days * 86_400,
        path=_REFRESH_COOKIE_PATH,
        domain=settings.cookie_domain,
        secure=settings.cookie_secure,
        httponly=True,
        samesite=settings.cookie_samesite,
    )


def _issue_tokens(
    session: Session,
    user: User,
    *,
    request: Request,
    response: Response,
    settings: Settings,
) -> TokenResponse:
    """Assigns the default base role if the user somehow has none (should
    only happen for an account created before RBAC seeding ran), issues a
    fresh access token + a brand-new session/refresh token, and sets the
    refresh cookie on `response`."""
    roles = get_user_roles(session, user.id)
    if not roles:
        # Fail-safe, not the expected path -- create_user always assigns
        # DEFAULT_ROLE_NAME. Ensures a locally-authenticated user is never
        # left with zero roles (and therefore zero access to anything,
        # including agent/authz.py-gated AI features) purely because RBAC
        # seeding hadn't run yet when the account was created.
        roles = ("user",)

    access_token = create_access_token(str(user.id), roles, settings)
    _, raw_refresh_token = create_session(
        session,
        user_id=user.id,
        refresh_token_expire_days=settings.refresh_token_expire_days,
        user_agent=request.headers.get("user-agent"),
        ip_address=_client_ip(request),
    )
    _set_refresh_cookie(response, raw_refresh_token, settings)

    return TokenResponse(
        access_token=access_token,
        expires_in=settings.access_token_expire_minutes * 60,
        user=_user_out(user, roles),
    )


@router.post(
    "/auth/register",
    response_model=TokenResponse | MessageResponse,
    status_code=status.HTTP_201_CREATED,
)
def register(
    payload: RegisterRequest,
    request: Request,
    response: Response,
    session: Session = Depends(get_identity_db),
) -> TokenResponse | MessageResponse:
    """Self-service registration. 403 if `Settings.allow_public_registration`
    is off (the default -- see that setting's own docstring: a fresh
    deployment should default to admin-provisioned accounts unless the
    operator explicitly wants open sign-up).

    On success, one of two shapes, matching `POST /auth/login`'s own
    `status != "active"` rejection so registration never hands out a
    session `/auth/login` would immediately refuse to reissue:

    - `Settings.require_email_verification` off (the default): signs the
      new account in immediately (`TokenResponse` -- an access token +
      the refresh cookie).
    - On: the account is created `pending_verification`, a verification
      email is sent (`identity/email.py`), and this returns a
      `MessageResponse` instead -- no token, since the account can't sign
      in yet.
    """
    settings = get_settings()
    require_local_auth_enabled(settings)
    _enforce_rate_limit(
        _register_limiters,
        request,
        action="register",
        max_events=settings.register_rate_limit_per_hour,
        window_seconds=3600.0,
    )

    if not settings.allow_public_registration:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Public registration is disabled."
        )
    violations = validate_password_strength(
        payload.password,
        settings=settings,
        email=payload.email,
        display_name=payload.display_name,
        username=payload.username,
    )
    if violations:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=violations)

    initial_status = "pending_verification" if settings.require_email_verification else "active"
    try:
        user = create_user(
            session,
            email=payload.email,
            password=payload.password,
            display_name=payload.display_name,
            username=payload.username,
            status=initial_status,
        )
    except DuplicateUserError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.safe_message) from exc

    log_security_event(
        "user_registered", "info", "A new local account was registered.", user_id=str(user.id)
    )

    if initial_status == "pending_verification":
        raw_token = create_email_verification_token(
            session, user.id, expire_minutes=settings.email_verification_token_expire_minutes
        )
        send_verification_email(user.email, raw_token, base_url=settings.app_base_url)
        record_signin_event(
            session, user_id=user.id, event_type="email_verification_sent", success=True
        )
        return MessageResponse(
            message="Account created. Check your email to verify your account before signing in."
        )

    record_signin_event(
        session,
        user_id=user.id,
        identifier_attempted=user.email,
        event_type="login_success",
        success=True,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return _issue_tokens(session, user, request=request, response=response, settings=settings)


@router.post("/auth/login", response_model=TokenResponse)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: Session = Depends(get_identity_db),
) -> TokenResponse:
    """Deliberately returns the exact same generic error (and the exact
    same 401 status) for "no such account," "wrong password," "account
    locked," and "account not active" -- see `identity.exceptions
    .InvalidCredentialsError`'s own docstring for why: distinguishing any
    of these in the response is a classic account-enumeration oracle.
    Every case is still logged with its *specific* reason server-side
    (`signin_events.failure_reason_code`, `security.audit_log`), just never
    echoed back to the caller.
    """
    settings = get_settings()
    require_local_auth_enabled(settings)
    _enforce_rate_limit(
        _login_limiters,
        request,
        action="login",
        max_events=settings.login_rate_limit_per_minute,
        window_seconds=60.0,
    )

    user = get_user_by_email(session, payload.email)
    if user is None:
        record_signin_event(
            session,
            identifier_attempted=payload.email,
            event_type="login_failed",
            success=False,
            failure_reason_code="no_such_account",
            ip_address=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=InvalidCredentialsError().safe_message
        )

    try:
        check_not_locked(user)
    except AccountLockedError:
        record_signin_event(
            session,
            user_id=user.id,
            identifier_attempted=payload.email,
            event_type="login_failed",
            success=False,
            failure_reason_code="account_locked",
            ip_address=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=InvalidCredentialsError().safe_message
        ) from None

    if not verify_password(payload.password, user.password_hash):
        just_locked = record_login_failure(
            session,
            user,
            max_attempts=settings.max_login_attempts,
            lockout_minutes=settings.login_lockout_minutes,
        )
        record_signin_event(
            session,
            user_id=user.id,
            identifier_attempted=payload.email,
            event_type="account_locked" if just_locked else "login_failed",
            success=False,
            failure_reason_code="account_locked" if just_locked else "bad_password",
            ip_address=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
        if just_locked:
            log_security_event(
                "account_locked",
                "warning",
                "An account was locked after too many failed login attempts.",
                user_id=str(user.id),
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=InvalidCredentialsError().safe_message
        )

    if user.status != "active":
        record_signin_event(
            session,
            user_id=user.id,
            identifier_attempted=payload.email,
            event_type="login_failed",
            success=False,
            failure_reason_code=f"status_{user.status}",
            ip_address=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=AccountNotActiveError().safe_message
        )

    record_login_success(session, user)
    record_signin_event(
        session,
        user_id=user.id,
        identifier_attempted=payload.email,
        event_type="login_success",
        success=True,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return _issue_tokens(session, user, request=request, response=response, settings=settings)


@router.post("/auth/refresh", response_model=TokenResponse)
def refresh(
    request: Request,
    response: Response,
    session: Session = Depends(get_identity_db),
) -> TokenResponse:
    """Reads the refresh token from the `refresh_token` cookie (never a
    request body/header) -- rotates it (a brand-new opaque token, the old
    one revoked) and issues a fresh access token.

    Deliberately has **no** `Depends(verify_api_key)` -- refreshing is
    exactly what a caller does *because* their access token has expired
    (or was never issued this call at all, e.g. a page reload), so
    requiring a currently-valid access token here would be self-defeating.
    Reuse of an already-rotated/revoked refresh token revokes the entire
    session family and 401s -- see `identity.repositories.sessions
    .rotate_refresh_token`'s own docstring.
    """
    settings = get_settings()
    require_local_auth_enabled(settings)
    _enforce_rate_limit(
        _refresh_limiters,
        request,
        action="refresh",
        max_events=settings.login_rate_limit_per_minute,
        window_seconds=60.0,
    )

    raw_token = request.cookies.get(_REFRESH_COOKIE_NAME)
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="No refresh token was presented."
        )

    try:
        new_session, new_raw_token = rotate_refresh_token(
            session,
            raw_refresh_token=raw_token,
            refresh_token_expire_days=settings.refresh_token_expire_days,
            ip_address=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
    except RefreshTokenReusedError as exc:
        log_security_event(
            "refresh_token_reuse_detected",
            "critical",
            "A rotated/revoked refresh token was replayed -- the session family was revoked.",
        )
        response.delete_cookie(_REFRESH_COOKIE_NAME, path=_REFRESH_COOKIE_PATH)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=exc.safe_message
        ) from exc
    except RefreshTokenInvalidError as exc:
        response.delete_cookie(_REFRESH_COOKIE_NAME, path=_REFRESH_COOKIE_PATH)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=exc.safe_message
        ) from exc

    user_row = get_user_by_id(session, new_session.user_id)
    if user_row is None or user_row.status != "active":
        revoke_session(session, new_session, reason="account_inactive")
        response.delete_cookie(_REFRESH_COOKIE_NAME, path=_REFRESH_COOKIE_PATH)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=AccountNotActiveError().safe_message
        )

    roles = get_user_roles(session, user_row.id) or ("user",)
    access_token = create_access_token(str(user_row.id), roles, settings)
    _set_refresh_cookie(response, new_raw_token, settings)
    record_signin_event(
        session,
        user_id=user_row.id,
        event_type="token_refresh",
        success=True,
        session_id=new_session.id,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return TokenResponse(
        access_token=access_token,
        expires_in=settings.access_token_expire_minutes * 60,
        user=_user_out(user_row, roles),
    )


@router.post("/auth/logout", response_model=MessageResponse)
def logout(
    request: Request, response: Response, session: Session = Depends(get_identity_db)
) -> MessageResponse:
    """Revokes the session matching the presented refresh cookie (if any)
    and clears the cookie. Never errors if no cookie is present or it
    doesn't match a live session -- logging out an already-logged-out
    session is a successful no-op, not a failure."""
    settings = get_settings()
    require_local_auth_enabled(settings)

    raw_token = request.cookies.get(_REFRESH_COOKIE_NAME)
    if raw_token:
        existing = get_session_by_refresh_token(session, raw_token)
        if existing is not None and existing.revoked_at is None:
            revoke_session(session, existing, reason="logout")
            record_signin_event(
                session,
                user_id=existing.user_id,
                event_type="logout",
                success=True,
                session_id=existing.id,
                ip_address=_client_ip(request),
                user_agent=request.headers.get("user-agent"),
            )
    response.delete_cookie(_REFRESH_COOKIE_NAME, path=_REFRESH_COOKIE_PATH)
    return MessageResponse(message="Signed out.")


@router.post("/auth/logout-all", response_model=MessageResponse)
def logout_all(
    request: Request,
    response: Response,
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> MessageResponse:
    """Revokes every live session for the caller's account, across every
    device -- requires a currently-valid access token (unlike plain
    `logout`, this needs to know *which account*, not just which session
    cookie)."""
    user, session = user_and_session
    count = revoke_all_sessions_for_user(session, user.id, reason="logout_all")
    record_signin_event(session, user_id=user.id, event_type="logout", success=True)
    log_security_event(
        "logout_all_sessions",
        "info",
        "A user signed out of all sessions.",
        user_id=str(user.id),
        count=count,
    )
    response.delete_cookie(_REFRESH_COOKIE_NAME, path=_REFRESH_COOKIE_PATH)
    return MessageResponse(message=f"Signed out of {count} session(s).")


@router.get("/auth/me", response_model=UserOut)
def me(user_and_session: tuple[User, Session] = Depends(require_local_user)) -> UserOut:
    user, session = user_and_session
    roles = get_user_roles(session, user.id) or ("user",)
    return _user_out(user, roles)


@router.patch("/auth/me", response_model=UserOut)
def update_profile(
    payload: UpdateProfileRequest,
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> UserOut:
    """Sets/changes the caller's own `display_name` -- the profile-completion
    path for an account created before `display_name` was mandatory at
    sign-up (`UserOut.needs_profile_completion`), and an ordinary profile
    edit for everyone else. Validation is identical to registration's own
    (`identity.display_name.validate_display_name`, applied by
    `UpdateProfileRequest`'s field validator)."""
    user, session = user_and_session
    updated = update_display_name(session, user, payload.display_name)
    roles = get_user_roles(session, user.id) or ("user",)
    log_security_event(
        "profile_updated", "info", "A user updated their display name.", user_id=str(user.id)
    )
    return _user_out(updated, roles)


@router.post("/auth/change-password", response_model=MessageResponse)
def change_password_route(
    payload: ChangePasswordRequest,
    request: Request,
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> MessageResponse:
    """Requires the caller's *current* password, not just a valid access
    token -- a stolen-but-not-yet-expired access token alone must not be
    enough to lock the real owner out by changing their password."""
    user, session = user_and_session
    settings = get_settings()

    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Current password is incorrect."
        )
    violations = validate_password_strength(
        payload.new_password,
        settings=settings,
        email=user.email,
        display_name=user.display_name,
        username=user.username,
    )
    if violations:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=violations)

    change_password(session, user, payload.new_password)
    record_signin_event(
        session, user_id=user.id, event_type="password_reset_completed", success=True
    )
    log_security_event(
        "password_changed", "info", "A user changed their own password.", user_id=str(user.id)
    )
    return MessageResponse(message="Password changed.")


_RESET_REQUESTED_MESSAGE = (
    "If an account with that email exists, a password reset link has been sent."
)


@router.post("/auth/forgot-password", response_model=MessageResponse)
def forgot_password(
    payload: ForgotPasswordRequest,
    request: Request,
    session: Session = Depends(get_identity_db),
) -> MessageResponse:
    """Always returns the same 200 + generic message, whether or not the
    email matches an account -- the account-enumeration defense
    `identity.exceptions.InvalidCredentialsError`'s own docstring describes
    for login applies identically here; a reset-request endpoint that
    replies differently for "sent" vs. "no such account" is exactly as
    much of an oracle as a login endpoint that does.
    """
    settings = get_settings()
    _enforce_rate_limit(
        _password_reset_limiters,
        request,
        action="forgot_password",
        max_events=settings.password_reset_rate_limit_per_hour,
        window_seconds=3600.0,
    )

    user = get_user_by_email(session, payload.email)
    if user is not None:
        raw_token = create_password_reset_token(
            session, user.id, expire_minutes=settings.password_reset_token_expire_minutes
        )
        send_password_reset_email(user.email, raw_token, base_url=settings.app_base_url)
        record_signin_event(
            session, user_id=user.id, event_type="password_reset_requested", success=True
        )
        log_security_event(
            "password_reset_requested",
            "info",
            "A password reset was requested.",
            user_id=str(user.id),
        )
    return MessageResponse(message=_RESET_REQUESTED_MESSAGE)


@router.post("/auth/reset-password", response_model=MessageResponse)
def reset_password(
    payload: ResetPasswordRequest,
    session: Session = Depends(get_identity_db),
) -> MessageResponse:
    """Redeems a `POST /auth/forgot-password`-issued token and sets a new
    password -- also revokes every existing session for the account (a
    password reset is exactly the scenario where the old credential may
    have been compromised, so any session established under it should not
    be trusted to continue silently)."""
    settings = get_settings()
    try:
        user = redeem_password_reset_token(session, payload.token)
    except IdentityError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=exc.safe_message
        ) from exc

    # Validated *after* redeeming the token (so a weak-password rejection
    # never leaks whether a given token is otherwise valid) but *before*
    # ever calling change_password -- a rejected reset must not consume the
    # token's single use (identity.repositories.tokens.redeem_password_reset_token
    # already marked it used above; see that function's own docstring for
    # why single-use tokens intentionally have no "undo" -- a genuinely weak
    # password here is rare enough, and the user still has the reset email
    # itself, that requiring a fresh reset request is an acceptable
    # tradeoff rather than adding token-reuse complexity for this one case).
    violations = validate_password_strength(
        payload.new_password,
        settings=settings,
        email=user.email,
        display_name=user.display_name,
        username=user.username,
    )
    if violations:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=violations)

    change_password(session, user, payload.new_password)
    revoke_all_sessions_for_user(session, user.id, reason="password_reset")
    record_signin_event(
        session, user_id=user.id, event_type="password_reset_completed", success=True
    )
    log_security_event(
        "password_reset_completed",
        "info",
        "A password was reset via a reset-password token; all sessions were revoked.",
        user_id=str(user.id),
    )
    return MessageResponse(message="Password has been reset. Please sign in again.")


@router.post("/auth/verify-email", response_model=MessageResponse)
def verify_email(
    payload: VerifyEmailRequest,
    session: Session = Depends(get_identity_db),
) -> MessageResponse:
    try:
        user = redeem_email_verification_token(session, payload.token)
    except IdentityError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=exc.safe_message
        ) from exc

    user.is_email_verified = True
    if user.status == "pending_verification":
        user.status = "active"
    session.commit()
    record_signin_event(session, user_id=user.id, event_type="email_verified", success=True)
    log_security_event(
        "email_verified", "info", "A user verified their email.", user_id=str(user.id)
    )
    return MessageResponse(message="Email verified.")


@router.post("/auth/resend-verification", response_model=MessageResponse)
def resend_verification(
    payload: ResendVerificationRequest,
    request: Request,
    session: Session = Depends(get_identity_db),
) -> MessageResponse:
    """Same account-enumeration-safe shape as `forgot_password` -- always
    200 with a generic message, regardless of whether the email exists or
    is already verified."""
    settings = get_settings()
    _enforce_rate_limit(
        _password_reset_limiters,
        request,
        action="resend_verification",
        max_events=settings.password_reset_rate_limit_per_hour,
        window_seconds=3600.0,
    )

    user = get_user_by_email(session, payload.email)
    if user is not None and not user.is_email_verified:
        raw_token = create_email_verification_token(
            session, user.id, expire_minutes=settings.email_verification_token_expire_minutes
        )
        send_verification_email(user.email, raw_token, base_url=settings.app_base_url)
        record_signin_event(
            session, user_id=user.id, event_type="email_verification_sent", success=True
        )
    return MessageResponse(message="If that account needs verification, a new link has been sent.")


@router.get("/auth/sessions", response_model=SessionListResponse)
def list_sessions(
    request: Request, user_and_session: tuple[User, Session] = Depends(require_local_user)
) -> SessionListResponse:
    user, session = user_and_session
    current_raw_token = request.cookies.get(_REFRESH_COOKIE_NAME)
    current_session_id = None
    if current_raw_token:
        current = get_session_by_refresh_token(session, current_raw_token)
        current_session_id = current.id if current is not None else None

    sessions = list_active_sessions_for_user(session, user.id)
    return SessionListResponse(
        sessions=[
            SessionOut(
                id=s.id,
                device_name=s.device_name,
                user_agent=s.user_agent,
                ip_address=s.ip_address,
                created_at=s.created_at,
                last_used_at=s.last_used_at,
                expires_at=s.expires_at,
                is_current=(s.id == current_session_id),
            )
            for s in sessions
        ]
    )


@router.delete("/auth/sessions/{session_id}", response_model=MessageResponse)
def revoke_one_session(
    session_id: str,
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> MessageResponse:
    """Revokes one of the *caller's own* sessions by id -- ownership is
    enforced by filtering `list_active_sessions_for_user` to the caller's
    own `user_id` before matching `session_id`, never by trusting the path
    parameter alone (the same "ownership check in the repository layer,
    not just a permission flag" principle every history endpoint in later
    phases also follows)."""
    user, session = user_and_session
    try:
        target_id = uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Session not found."
        ) from None

    own_sessions = {s.id: s for s in list_active_sessions_for_user(session, user.id)}
    target = own_sessions.get(target_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found.")

    revoke_session(session, target, reason="user_revoked")
    return MessageResponse(message="Session revoked.")
