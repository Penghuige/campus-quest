# backend/app/integrations/email.py
"""Email port: template-based sends with typed delivery records.

Design choice (recorded per task brief): `send` takes a template plus
variables — not subject/body — matching the frozen port shape in
docs/architecture/interfaces.md and the notification module's centralized
template rendering (Plan 07). Free-form subject/body composition would
scatter rendering across callers. Tests assert exact deliveries via
`FakeEmailSender.messages`.

`LoggingEmailSender` is the interim production adapter (Plan 02): real
provider delivery arrives with the notification module (Plan 07), so until
then the composition root wires a sender that logs the masked recipient
and template only — NEVER `variables` (the verification token travels
there) — instead of silently dropping the send.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from app.integrations.masking import mask_email

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SentEmail:
    """One accepted email send, exactly as the fake records it."""

    to: str
    template: str
    variables: Mapping[str, Any]


class EmailSender(Protocol):
    """Port for sending templated emails."""

    def send(self, *, to: str, template: str, variables: Mapping[str, Any]) -> None:
        """Send one templated email.

        Args:
            to: Recipient email address (verified by the identity module
                before eligibility, not by this port).
            template: Template identifier (for example
                `"revision_required"`); subject and body are rendered from
                centrally managed templates, never passed inline.
            variables: Render inputs for the template; values must be
                JSON-serializable.

        Raises:
            TemporaryProviderError: transient failure; bounded retry safe.
            PermanentProviderError: provider rejected the recipient/content.
            UnknownOutcomeError: timeout; retry only with idempotency key.
        """
        ...


class LoggingEmailSender:
    """Interim `EmailSender` adapter: log masked, deliver nothing (Plan 02).

    Plan 07 replaces this at the composition root when the notification
    module ships real provider adapters. Deliberately never raises.
    """

    def send(self, *, to: str, template: str, variables: Mapping[str, Any]) -> None:
        logger.info(
            "email send (interim logging adapter) to=%s template=%s",
            mask_email(to),
            template,
        )
