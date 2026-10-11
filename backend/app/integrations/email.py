# backend/app/integrations/email.py
"""Email port: template-based sends with typed delivery records.

Design choice (frozen port shape in
docs/architecture/interfaces.md): `send` takes a template plus
variables — not subject/body — matching the notification module's
centralized template rendering. Free-form subject/body composition would
scatter rendering across callers. Tests assert exact deliveries via
`FakeEmailSender.messages`.

`LoggingEmailSender` is the development-only adapter: the real adapter
lives in `integrations/email_smtp.py` ("smtp", security round batch 4
item 4), and the composition root (`build_email_sender`, wired from
`Settings.email_provider`) resolves it when the SMTP_* group is set.
The logging sender logs the masked recipient and template only — NEVER
`variables` (the verification token travels there) — instead of silently
dropping the send. Its receipt carries the "logging:" prefix so recorded
deliveries distinguish simulated sends from real provider receipts;
production never runs it (config.py's production guard refuses
provider="logging" at Settings construction — fail closed, G4/G5).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from app.integrations.masking import mask_email

logger = logging.getLogger(__name__)

#: Prefix marking a receipt as a simulated logging send (see sms.py).
LOGGING_RECEIPT_PREFIX = "logging:"


@dataclass(frozen=True)
class SentEmail:
    """One accepted email send, exactly as the fake records it.

    `idempotency_key` is None for callers that send single-shot (the
    identity verification flow); notification delivery always passes
    one.
    """

    to: str
    template: str
    variables: Mapping[str, Any]
    idempotency_key: str | None = None


class EmailSender(Protocol):
    """Port for sending templated emails."""

    def send(
        self,
        *,
        to: str,
        template: str,
        variables: Mapping[str, Any],
        idempotency_key: str | None = None,
    ) -> str | None:
        """Send one templated email.

        Args:
            to: Recipient email address (verified by the identity module
                before eligibility, not by this port).
            template: Template identifier (for example
                `"revision_required"`); subject and body are rendered from
                centrally managed templates, never passed inline.
            variables: Render inputs for the template; values must be
                JSON-serializable.
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


def build_email_sender(provider: str, settings: Any = None) -> EmailSender:
    """The `EmailSender` adapter a configured provider value wires.

    The fail-closed chain (PR #2 hardening P0-2): config.py's production
    guard refuses provider="logging" when environment=production, so the
    logging branch below is development-only by construction — there is
    no silent fallback onto a sender that fakes success. "smtp" is the
    real adapter (security round batch 4 item 4, integrations/
    email_smtp.py): selecting it requires the SMTP_* settings group —
    incomplete credentials fail at composition in EVERY environment
    (the aliyun_dypns discipline: a half-configured provider never
    reaches a first send).
    """
    if provider == "logging":
        return LoggingEmailSender()
    if provider == "smtp" and settings is not None:
        from app.integrations.email_smtp import SmtpEmailSender

        missing = [
            field_name
            for field_name in (
                "smtp_host",
                "smtp_username",
                "smtp_password",
                "smtp_from_address",
            )
            if not getattr(settings, field_name, "")
        ]
        if missing:
            # Every environment, not just production: a half-configured
            # real provider fails HERE, at composition, instead of at
            # the first send.
            raise LookupError(
                f"email_provider=smtp is missing configuration: "
                f"{', '.join(missing)} (set the SMTP_* env)"
            )
        return SmtpEmailSender(
            host=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password,
            from_address=settings.smtp_from_address,
            use_tls=settings.smtp_use_tls,
        )
    if provider == "smtp":
        raise LookupError(
            "email_provider=smtp needs the deployment settings "
            "(wired from the composition root, not constructed bare)"
        )
    raise LookupError(
        f"unknown email_provider {provider!r}: extend the email_provider "
        "Literal in app/core/config.py together with this factory"
    )


class LoggingEmailSender:
    """Development-only `EmailSender` adapter: log masked, deliver nothing.

    Production never runs this adapter: config.py's production guard
    refuses provider="logging" at Settings construction, so the wiring
    cannot reach it there (fail closed). Each accepted send returns a
    `"logging:"<uuid>` receipt so the recorded delivery carries a
    provider_message_id clearly marked as simulated. Deliberately never
    raises.
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
            "email send (interim logging adapter) to=%s template=%s "
            "idempotency_key=%s receipt=%s",
            mask_email(to),
            template,
            idempotency_key,
            receipt,
        )
        return receipt
