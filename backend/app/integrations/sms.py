# backend/app/integrations/sms.py
"""SMS port: template-based sends with typed delivery records.

Domain modules call `SmsSender.send`; provider specifics (Aliyun, Twilio,
...) live in real adapters, not here. Tests assert exact deliveries via
`FakeSmsSender.messages` (docs/architecture/interfaces.md, Adapter Ports).

`LoggingSmsSender` is the development-only adapter: the composition root
(`build_sms_sender`, wired from `Settings.sms_provider`) resolves the
real provider adapter when one is configured — SMS offers `aliyun_dypns`
(integrations/sms_aliyun.py) — and otherwise the sender that logs the
masked recipient and template only — NEVER
`variables` (the OTP code travels there, spec §33.2) — instead of
silently dropping the send. Its receipt carries the "logging:" prefix so
recorded deliveries distinguish simulated sends from real provider
receipts; production never runs it (config.py's production guard refuses
provider="logging" at Settings construction — fail closed, G4/G5).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from app.integrations.masking import mask_phone

logger = logging.getLogger(__name__)

#: Prefix marking a receipt as a simulated logging send, so operations
#: can tell a "logging:"<uuid> provider_message_id from a real provider
#: receipt at a glance.
LOGGING_RECEIPT_PREFIX = "logging:"


@dataclass(frozen=True)
class SentSms:
    """One accepted SMS send, exactly as the fake records it.

    `variables` is the render-input snapshot; field equality (including
    `variables` as a mapping) is the exact-delivery contract.
    `idempotency_key` is None for callers that send single-shot (the
    identity OTP flow); notification delivery always passes one.
    """

    to: str
    template: str
    variables: Mapping[str, Any]
    idempotency_key: str | None = None


class SmsSender(Protocol):
    """Port for sending templated SMS notifications."""

    def send(
        self,
        *,
        to: str,
        template: str,
        variables: Mapping[str, Any],
        idempotency_key: str | None = None,
    ) -> str | None:
        """Send one templated SMS.

        Args:
            to: E.164 recipient phone number.
            template: Provider template identifier (for example
                `"deadline_4h"`), never free-form message text.
            variables: Render inputs for the provider template; values must
                be JSON-serializable.
            idempotency_key: Optional dedupe key the provider can collapse
                repeated sends onto (notification delivery passes
                `"{event_key}:{channel}:{user_id}"` so an
                UnknownOutcomeError retry cannot double-send, spec
                §25.3). Single-shot callers omit it.

        Returns:
            The provider receipt id for the accepted send, or None when
            the adapter surfaces no receipt. The logging adapter returns
            a "logging:"-prefixed id marking the send as simulated.

        Raises:
            TemporaryProviderError: transient failure; bounded retry safe.
            PermanentProviderError: provider rejected the recipient/content.
            UnknownOutcomeError: timeout; retry only with idempotency key.
        """
        ...


def build_sms_sender(provider: str, settings: Any = None) -> SmsSender:
    """The `SmsSender` adapter a configured provider value wires.

    The fail-closed chain (PR #2 hardening P0-2): config.py's production
    guard refuses provider="logging" when environment=production, so the
    logging branch below is development-only by construction — there is
    no silent fallback onto a sender that fakes success. The real
    adapter ("aliyun_dypns", integrations/sms_aliyun.py) refuses
    incomplete credentials HERE in every environment — a misconfigured
    deployment fails at composition, never at the first send.

    ``settings`` is the composition root's ``Settings`` (typed Any to
    keep the integrations layer free of the config import cycle); the
    logging branch ignores it.
    """
    if provider == "logging":
        return LoggingSmsSender()
    if provider == "aliyun_dypns" and settings is not None:
        from app.integrations.sms_aliyun import AliyunDypnsSmsSender

        missing = [
            field_name
            for field_name in (
                "aliyun_sms_access_key_id",
                "aliyun_sms_access_key_secret",
                "aliyun_sms_sign_name",
                "aliyun_sms_otp_template_code",
            )
            if not getattr(settings, field_name, "")
        ]
        if missing:
            # Every environment, not just production: a half-configured
            # real provider fails HERE, at composition, instead of at
            # the first send (which would burn a real outbound attempt
            # on a request signed with garbage).
            raise LookupError(
                f"sms_provider=aliyun_dypns is missing credentials: "
                f"{', '.join(missing)} (set the ALIYUN_SMS_* env)"
            )
        return AliyunDypnsSmsSender(
            access_key_id=settings.aliyun_sms_access_key_id,
            access_key_secret=settings.aliyun_sms_access_key_secret,
            sign_name=settings.aliyun_sms_sign_name,
            otp_template_code=settings.aliyun_sms_otp_template_code,
            endpoint=settings.aliyun_sms_endpoint,
            region=settings.aliyun_sms_region,
        )
    if provider == "aliyun_dypns":
        raise LookupError(
            "sms_provider=aliyun_dypns needs the deployment settings "
            "(wired from the composition root, not constructed bare)"
        )
    raise LookupError(
        f"unknown sms_provider {provider!r}: extend the sms_provider "
        "Literal in app/core/config.py together with this factory"
    )


class LoggingSmsSender:
    """Development-only `SmsSender` adapter: log masked, deliver nothing.

    Production never runs this adapter: config.py's production guard
    refuses provider="logging" at Settings construction, so the wiring
    cannot reach it there (fail closed). Each accepted send returns a
    `"logging:"<uuid>` receipt so the recorded delivery carries a
    provider_message_id clearly marked as simulated. Deliberately never
    raises: a dev-deployment log sink failing must not fail the request.
    """

    def send(
        self,
        *,
        to: str,
        template: str,
        variables: Mapping[str, Any],
        idempotency_key: str | None = None,
    ) -> str:
        receipt = f"{LOGGING_RECEIPT_PREFIX}{uuid4()}"
        logger.info(
            "sms send (interim logging adapter) to=%s template=%s "
            "idempotency_key=%s receipt=%s",
            mask_phone(to),
            template,
            idempotency_key,
            receipt,
        )
        return receipt
