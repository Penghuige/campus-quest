"""Cookie paths must follow the external API prefix (deployment mount).

When the API is reverse-proxied under a public path prefix (e.g.
https://host/campus → backend /api/v1), the browser-visible URL of the
auth endpoints is /campus/api/v1/auth/*, so the cookies' Path attributes
must carry the same prefix — a cookie scoped to /api/v1/auth would never
be sent back by the browser and refresh/logout would silently break.
Default deployments (no prefix) keep the historical paths.
"""

from fastapi import Response

from app.core.config import Settings, get_settings
from app.modules.identity.routing_common import (
    _issue_session_cookies,
    auth_cookie_path,
    clear_session_cookies,
    csrf_cookie_path,
)
from app.modules.identity.session_service import SessionTokens


def _tokens() -> SessionTokens:
    return SessionTokens(access_token="at", refresh_token="rt")


def _settings(prefix: str) -> Settings:
    return get_settings().model_copy(update={"external_api_prefix": prefix})


def _cookies(response: Response) -> list[str]:
    return [c.lower() for c in response.headers.getlist("set-cookie")]


def test_default_paths_unchanged_without_prefix() -> None:
    settings = _settings("")
    assert auth_cookie_path(settings) == "/api/v1/auth"
    assert csrf_cookie_path(settings) == "/"

    response = Response()
    _issue_session_cookies(response, _tokens(), settings)
    cookies = _cookies(response)
    assert any("path=/api/v1/auth" in c for c in cookies)
    assert any(c.startswith("csrf_token=") and "path=/" in c for c in cookies)

    clear = Response()
    clear_session_cookies(clear, settings)
    cleared = _cookies(clear)
    assert any("path=/api/v1/auth" in c for c in cleared)
    assert any(c.startswith("csrf_token=") and "path=/" in c for c in cleared)


def test_prefixed_mount_scopes_both_cookies_under_prefix() -> None:
    settings = _settings("/campus")
    assert auth_cookie_path(settings) == "/campus/api/v1/auth"
    assert csrf_cookie_path(settings) == "/campus/"

    response = Response()
    _issue_session_cookies(response, _tokens(), settings)
    cookies = _cookies(response)
    # The refresh cookie must NOT fall back to the unprefixed path: the
    # browser would never send it to /campus/api/v1/auth/refresh.
    refresh = [c for c in cookies if c.startswith("refresh_token=")]
    assert refresh and "path=/campus/api/v1/auth" in refresh[0]
    csrf = [c for c in cookies if c.startswith("csrf_token=")]
    assert csrf and "path=/campus/" in csrf[0]

    clear = Response()
    clear_session_cookies(clear, settings)
    cleared = _cookies(clear)
    assert any("path=/campus/api/v1/auth" in c for c in cleared)
    assert any("path=/campus/" in c for c in cleared)
