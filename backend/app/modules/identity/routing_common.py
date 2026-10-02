# backend/app/modules/identity/routing_common.py
"""HTTP plumbing shared by the three identity routers (auth/profile/staff).

Single responsibility: the session-cookie/CSRF contract, the small
route-level helpers every identity router reuses, and the module's
typed-exception envelope mapping. Routes themselves live in
``auth_router`` / ``profile_router`` / ``staff_router``; the service
providers live in ``providers``.

Spec §5.6, §28, §29, §33.1, §33.4; backend-engineering §3 (router
standard), §9 (DTO separation).

- **Refresh token cookie + CSRF double-submit.** Login/rotation set the
  refresh token as an HttpOnly + Secure + SameSite=Lax cookie scoped to
  the auth paths (spec §5.6 推荐), together with a NON-HttpOnly CSRF
  cookie carrying an independent secure random token. Cookie-authenticated
  mutations (refresh, logout) must echo that token in ``X-CSRF-Token``;
  the comparison is constant-time. A non-browser client that captured the
  cookie value may send the refresh token in the body instead and never
  receives the CSRF obligation — the dependency only engages when the
  refresh cookie is present, so a body-token request carries no ambient
  cookie authority to forge. DELIVERY is cookie-only:
  ``TokenPairResponse`` carries the short-lived access token plus the
  ``csrf_token`` mirror of the readable cookie — never the refresh token.
  The V1 client is the cookie-using Next.js PWA; a deliberate token-client
  contract can be added later if one is ever needed.
- **Endpoint rate limiting (spec §33.1).** login / staff-login / register
  / OTP-send / email-verify / phone-change / password-reset check the
  ``RateLimiter`` port with NORMALIZED identifiers (stripped+lowercased
  username/email, E.164 phone, stripped student number) before the
  service call, using the shared rules in
  ``app.integrations.rate_limit.RATE_LIMIT_RULES``. Exhausted windows
  render the 429 ``RATE_LIMITED`` envelope. This layers on top of the OTP
  lifecycle's own caps (§33.2); it does not replace them.
- **Typed-exception mapping, registered once.** The services raise typed
  module exceptions (``otp``'s taxonomy, ``TotpSetupRequiredError``, the
  email errors) because the §29 registry had no codes when they landed.
  This module registers one FastAPI exception handler per type, rendering
  the frozen envelope with the doc-first codes registered in
  docs/architecture/interfaces.md
  ("Identity typed-exception mapping"). Routes never translate errors.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.requests import Request as StarletteRequest

from app.core.config import Settings
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError, error_envelope
from app.core.observability import REQUEST_ID_HEADER
from app.core.security import constant_time_equals
from app.integrations.rate_limit import (
    RATE_LIMIT_RULES,
    RateLimiter,
    RateLimitExceededError,
)
from app.modules.identity.email_verification import (
    EmailAlreadyBoundError,
    InvalidEmailTokenError,
)
from app.modules.identity.otp import (
    ChallengeAlreadyConsumedError,
    ChallengeExpiredError,
    InvalidPhoneError,
    InvalidTokenError,
    OtpRateLimitError,
    ResendCooldownError,
    TooManyAttemptsError,
    UnknownChallengeError,
    WrongCodeError,
)
from app.modules.identity.schemas import TokenPairResponse
from app.modules.identity.session_service import SessionTokens
from app.modules.identity.staff_service import TotpSetupRequiredError

# --- Cookie / CSRF contract (spec §5.6, §33.1) --------------------------------

REFRESH_COOKIE_NAME = "refresh_token"
CSRF_COOKIE_NAME = "csrf_token"
CSRF_HEADER_NAME = "X-CSRF-Token"


def auth_cookie_path(settings: Settings) -> str:
    """The refresh cookie's Path, under the deployment's public prefix.

    The refresh cookie is only ever read by the auth endpoints; scoping its
    path shrinks what any other endpoint (or bug) can see. A prefixed
    mount (``external_api_prefix``) must carry that prefix, or the browser
    would never send the cookie back to the proxied auth URLs.
    """
    prefix = settings.external_api_prefix
    return f"{prefix}/api/v1/auth" if prefix else "/api/v1/auth"


def csrf_cookie_path(settings: Settings) -> str:
    """The readable CSRF cookie's Path — the app's public mount scope.

    No trailing slash on the prefixed form: RFC 6265 path-match does
    NOT match the bare mount root (/campus/) when the gateway
    normalizes away the trailing slash, so the root page would lose
    the double-submit cookie and every cookie-authed refresh 403s
    (production finding, PR #14 QA round). path=/campus covers BOTH
    /campus and every /campus/... subpath.
    """
    prefix = settings.external_api_prefix
    return prefix if prefix else "/"


_SECONDS_PER_DAY = 86400

_AUTHENTICATION_REQUIRED_MESSAGE = "未登录或登录状态已失效"
_CSRF_REJECTED_MESSAGE = "CSRF 校验失败：Cookie 请求必须携带有效的 X-CSRF-Token"


def require_csrf_when_cookie_bearer(request: Request) -> None:
    """Reject cookie-authenticated mutations without a matching CSRF token.

    Engages ONLY when the refresh cookie is presented: that cookie is the
    ambient browser credential a cross-site forge can carry. A request that
    authenticates purely by body (mobile clients) involves no ambient
    authority and skips the check. The double-submit pair is the non-HttpOnly
    ``csrf_token`` cookie and the ``X-CSRF-Token`` header, compared
    constant-time; absence on either side is the same rejection.
    """
    if REFRESH_COOKIE_NAME not in request.cookies:
        return
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    header_token = request.headers.get(CSRF_HEADER_NAME)
    if (
        not cookie_token
        or not header_token
        or not constant_time_equals(cookie_token, header_token)
    ):
        raise BusinessError(
            ErrorCode.PERMISSION_DENIED,
            _CSRF_REJECTED_MESSAGE,
            status_code=403,
        )


def _issue_session_cookies(
    response: Response, tokens: SessionTokens, settings: Settings
) -> str:
    """Set the refresh (HttpOnly) and CSRF (readable) cookies; return CSRF.

    Both cookies rotate with every issue, so a stale CSRF token from a
    previous session cannot authorize the next refresh. Spec §5.6/§33.1:
    Secure + SameSite=Lax on both; HttpOnly on the refresh cookie only —
    the CSRF token is double-submit material and must stay readable.
    """
    csrf_token = secrets.token_urlsafe(32)
    max_age = settings.refresh_token_ttl_days * _SECONDS_PER_DAY
    response.set_cookie(
        REFRESH_COOKIE_NAME,
        tokens.refresh_token,
        max_age=max_age,
        secure=True,
        httponly=True,
        samesite="lax",
        path=auth_cookie_path(settings),
    )
    response.set_cookie(
        CSRF_COOKIE_NAME,
        csrf_token,
        max_age=max_age,
        secure=True,
        samesite="lax",
        path=csrf_cookie_path(settings),
    )
    _expire_stale_trailing_slash_csrf(response, settings)
    return csrf_token


def _expire_stale_trailing_slash_csrf(
    response: Response, settings: Settings
) -> None:
    """Expire the legacy trailing-slash csrf cookie variant.

    Pre-fix sessions hold csrf_token@path=/<prefix>/ (with slash). RFC
    6265 §5.4 orders longer paths first, so document.cookie lists the
    stale variant before the fresh path=/<prefix> one — a frontend
    find() reads the WRONG value and sub-path refreshes 403 until the
    old cookie ages out. A Max-Age=0 at the exact stale path evicts it
    on the next auth response. No-op for root mounts and fresh visitors
    (no stale variant can exist).
    """
    prefix = settings.external_api_prefix
    if prefix:
        response.set_cookie(
            CSRF_COOKIE_NAME,
            "",
            max_age=0,
            secure=True,
            samesite="lax",
            path=f"{prefix}/",
        )


def clear_session_cookies(response: Response, settings: Settings) -> None:
    """Expire both auth cookies at the SAME paths they were set with.

    A delete at a different Path edits a different jar entry: the real
    cookie would survive. Both paths derive from the same settings the
    issue used, so logout always clears exactly what login set.
    """
    response.delete_cookie(REFRESH_COOKIE_NAME, path=auth_cookie_path(settings))
    response.delete_cookie(CSRF_COOKIE_NAME, path=csrf_cookie_path(settings))


def _token_pair_response(
    response: Response, tokens: SessionTokens, settings: Settings
) -> TokenPairResponse:
    csrf_token = _issue_session_cookies(response, tokens, settings)
    # Cookie-only refresh delivery: the long-lived refresh
    # token travels exclusively in the HttpOnly Set-Cookie header, never in
    # the JSON body. The body's access token is short-lived and bears no
    # refresh capability, so it is not the credential an XSS-leaked body
    # would prize.
    return TokenPairResponse(
        access_token=tokens.access_token,
        csrf_token=csrf_token,
    )


def _resolve_refresh_token(request: Request, body_token: str | None) -> str:
    """The presented refresh token: body first, then the cookie, else 401."""
    if body_token:
        return body_token
    cookie_token = request.cookies.get(REFRESH_COOKIE_NAME)
    if cookie_token:
        return cookie_token
    raise BusinessError(
        ErrorCode.AUTHENTICATION_REQUIRED,
        _AUTHENTICATION_REQUIRED_MESSAGE,
        status_code=401,
    )


def _client_ip(request: Request) -> str:
    return request.client.host if request.client is not None else "unknown"


async def _enforce_rate_limit(
    limiter: RateLimiter, bucket: str, identifier: str
) -> None:
    rule = RATE_LIMIT_RULES[bucket]
    await limiter.check(
        bucket=rule.bucket,
        identifier=identifier,
        limit=rule.limit,
        window_seconds=rule.window_seconds,
    )


# --- Typed-exception -> envelope mapping (docs/architecture/interfaces.md) ----

_TYPED_EXCEPTION_CODES: tuple[tuple[type[Exception], int, ErrorCode], ...] = (
    (TotpSetupRequiredError, 403, ErrorCode.TOTP_SETUP_REQUIRED),
    (EmailAlreadyBoundError, 409, ErrorCode.EMAIL_ALREADY_BOUND),
    (InvalidEmailTokenError, 400, ErrorCode.INVALID_EMAIL_TOKEN),
    (InvalidPhoneError, 400, ErrorCode.VALIDATION_ERROR),
    (WrongCodeError, 400, ErrorCode.OTP_CODE_INVALID),
    (TooManyAttemptsError, 429, ErrorCode.OTP_TOO_MANY_ATTEMPTS),
    (ChallengeExpiredError, 400, ErrorCode.OTP_CHALLENGE_EXPIRED),
    (ChallengeAlreadyConsumedError, 400, ErrorCode.OTP_CHALLENGE_CONSUMED),
    (UnknownChallengeError, 400, ErrorCode.OTP_CHALLENGE_INVALID),
    (InvalidTokenError, 400, ErrorCode.OTP_TOKEN_INVALID),
    (ResendCooldownError, 429, ErrorCode.OTP_RESEND_COOLDOWN),
    # Both rate-limit failures (OTP-internal caps and the endpoint limiter)
    # render the same code; the branch reason stays in server logs.
    (OtpRateLimitError, 429, ErrorCode.RATE_LIMITED),
    (RateLimitExceededError, 429, ErrorCode.RATE_LIMITED),
)


def register_identity_exception_handlers(app: FastAPI) -> None:
    """Attach one §29-envelope handler per typed identity exception.

    Rendered through the same envelope builder as ``core.errors`` so every
    response — business error, typed domain error, or framework failure —
    is indistinguishable in shape. Messages come from the exceptions (their
    Chinese UX strings), codes from the frozen registry.
    """

    def _handler(
        status_code: int, code: ErrorCode
    ) -> Callable[[StarletteRequest, Exception], Awaitable[JSONResponse]]:
        async def render(request: StarletteRequest, exc: Exception) -> JSONResponse:
            request_id = getattr(request.state, "request_id", None)
            headers = {REQUEST_ID_HEADER: request_id} if request_id else None
            return JSONResponse(
                status_code=status_code,
                content=error_envelope(code, str(exc), None, request_id),
                headers=headers,
            )

        return render

    for exception_type, status_code, code in _TYPED_EXCEPTION_CODES:
        app.add_exception_handler(exception_type, _handler(status_code, code))
