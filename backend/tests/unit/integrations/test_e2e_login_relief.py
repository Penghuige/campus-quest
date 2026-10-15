# backend/tests/unit/integrations/test_e2e_login_relief.py
"""The e2e-only login rate-limit relief switch (battery unblocking).

The e2e battery's staff login volume structurally exceeds the login
family buckets (131 POSTs vs 50/h on the IP bucket, retry loops feeding
back into the same bucket). The relief is OPT-IN per environment via
``CQ_E2E_LOGIN_RATELIMIT``: the local e2e orchestration stack sets it;
production and every default deployment see byte-identical limits.
"""

from __future__ import annotations

import pytest

from app.integrations import rate_limit


@pytest.fixture(autouse=True)
def _default_rules():
    """Every test leaves the process-wide table in its default shape."""
    rate_limit.restore_default_login_rules()
    yield
    rate_limit.restore_default_login_rules()


def test_relief_raises_every_login_family_bucket() -> None:
    login_buckets = {
        "auth:login",
        "auth:login-ip",
        "auth:staff-login",
        "auth:staff-login-ip",
        "auth:staff-email-code",
        "auth:staff-email-code-ip",
    }
    before = {bucket: rate_limit.RATE_LIMIT_RULES[bucket] for bucket in login_buckets}
    assert before["auth:staff-login"].limit == 10
    assert before["auth:staff-login-ip"].limit == 50

    rate_limit.apply_e2e_login_relief()

    for bucket in login_buckets:
        rule = rate_limit.RATE_LIMIT_RULES[bucket]
        assert rule.bucket == bucket
        assert rule.limit == 1000, bucket
        assert rule.window_seconds == 3600, bucket


def test_relief_touches_no_other_bucket() -> None:
    untouched = "auth:otp-send"
    before = rate_limit.RATE_LIMIT_RULES[untouched]

    rate_limit.apply_e2e_login_relief()

    assert rate_limit.RATE_LIMIT_RULES[untouched] == before


def test_relief_is_idempotent_and_restorable() -> None:
    defaults = dict(rate_limit.RATE_LIMIT_RULES)

    rate_limit.apply_e2e_login_relief()
    rate_limit.apply_e2e_login_relief()
    assert rate_limit.RATE_LIMIT_RULES["auth:staff-login"].limit == 1000

    rate_limit.restore_default_login_rules()
    assert defaults == rate_limit.RATE_LIMIT_RULES


def test_default_settings_keep_the_switch_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The contract pin: without the env var the flag is False — every
    default deployment (production included) keeps the real limits."""
    from app.core.config import Settings

    monkeypatch.delenv("CQ_E2E_LOGIN_RATELIMIT", raising=False)
    settings = Settings.model_construct()
    assert settings.e2e_login_ratelimit is False


def test_env_var_enables_the_switch(monkeypatch: pytest.MonkeyPatch) -> None:

    monkeypatch.setenv("CQ_E2E_LOGIN_RATELIMIT", "1")
    # Parse just the flag off a full construction path: the required
    # fields come from the test stack env the suite already exports.
    from app.core.config import get_settings

    get_settings.cache_clear()
    try:
        assert get_settings().e2e_login_ratelimit is True
    finally:
        get_settings.cache_clear()


def test_assembly_default_leaves_rules_untouched() -> None:
    """The full create_app path with the env unset: building the real app
    (as every integration fixture does in this process) must leave the
    login family at shipped defaults — the production posture."""
    from app.main import create_app

    create_app()  # default env in this suite: relief must NOT apply

    for bucket in rate_limit.LOGIN_FAMILY_BUCKETS:
        rule = rate_limit.RATE_LIMIT_RULES[bucket]
        assert rule == rate_limit._DEFAULT_LOGIN_RULES[bucket], bucket
