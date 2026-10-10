# backend/tests/unit/integrations/test_rate_limit_rules.py
"""The RATE_LIMIT_RULES table's anti-stuffing contracts (spec §33.1).

Pure data assertions: the login-family endpoints must carry PARALLEL
IP-keyed buckets next to their identifier buckets, because any
per-identifier cap is defeated by one address rotating through many
identifiers (hardening A-1). The wiring — that the routers actually run
both checks — is pinned by the identity API tests
(`test_login_family_checks_the_transport_ip_in_parallel`).
"""

from __future__ import annotations

from app.integrations.rate_limit import RATE_LIMIT_RULES

# The credential-checking endpoints whose aggregate per-IP volume must
# be capped alongside their per-identifier caps.
IP_PAIRED_BUCKETS = (
    "auth:login",
    "auth:staff-login",
    "auth:otp-send",
    "auth:otp-verify",
)

# The OTP-side per-IP hourly default (`otp_ip_hourly_request_limit`,
# spec §33.2): the shared-campus-NAT budget the IP buckets mirror.
IP_HOURLY_LIMIT = 50


def test_credential_buckets_carry_parallel_ip_keyed_rules() -> None:
    # One IP stuffing many usernames/emails/phones opens a fresh
    # identifier bucket per guess; only a ``<bucket>-ip`` rule bounds the
    # aggregate per transport peer. Missing rule = the stuffing hole
    # reopens silently, so the pairing is pinned per endpoint.
    for bucket in IP_PAIRED_BUCKETS:
        rule = RATE_LIMIT_RULES[f"{bucket}-ip"]
        assert rule.bucket == f"{bucket}-ip"
        assert rule.limit == IP_HOURLY_LIMIT
        assert rule.window_seconds == 3600


def test_otp_verify_has_its_own_identifier_bucket() -> None:
    # The verify endpoint gained its identifier layer together with the
    # IP layer: per challenge id (the OTP service already bounds code
    # attempts per challenge at 5, so 30/hour only anti-hammers the HTTP
    # surface), hourly to match the IP bucket's window family.
    rule = RATE_LIMIT_RULES["auth:otp-verify"]
    assert rule.limit == 30
    assert rule.window_seconds == 3600
