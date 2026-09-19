# backend/app/integrations/sms.py
"""SMS port: template-based sends with typed delivery records.

Domain modules call `SmsSender.send`; provider specifics (Aliyun, Twilio,
...) live in real adapters, not here. Tests assert exact deliveries via
`FakeSmsSender.messages` (docs/architecture/interfaces.md, Adapter Ports).

`LoggingSmsSender` is the interim production adapter (Plan 02): the
notification module (Plan 07) owns real provider delivery, so until it
lands the composition root wires a sender that logs the masked recipient
and template only — NEVER `variables` (the OTP code travels there, spec
§33.2) — instead of silently dropping the send.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SentSms:
    """One accepted SMS send, exactly as the fake records it.

    `variables` is the render-input snapshot; field equality (including
    `variables` as a mapping) is the exact-delivery contract.
    """

    to: str
    template: str
    variables: Mapping[str, Any]


class SmsSender(Protocol):
    """Port for sending templated SMS notifications."""

    def send(self, *, to: str, template: str, variables: Mapping[str, Any]) -> None:
        """Send one templated SMS.

        Args:
            to: E.164 recipient phone number.
            template: Provider template identifier (for example
                `"deadline_4h"`), never free-form message text.
            variables: Render inputs for the provider template; values must
                be JSON-serializable.

        Raises:
            TemporaryProviderError: transient failure; bounded retry safe.
            PermanentProviderError: provider rejected the recipient/content.
            UnknownOutcomeError: timeout; retry only with idempotency key.
        """
        ...


def _mask_phone(phone_e164: str) -> str:
    if len(phone_e164) <= 8:
        return f"{phone_e164[:2]}****"
    return f"{phone_e164[:3]}****{phone_e164[-4:]}"


class LoggingSmsSender:
    """Interim `SmsSender` adapter: log masked, deliver nothing (Plan 02).

    Plan 07 replaces this at the composition root when the notification
    module ships real provider adapters. Deliberately never raises: a
    dev-deployment log sink failing must not fail the request.
    """

    def send(self, *, to: str, template: str, variables: Mapping[str, Any]) -> None:
        logger.info(
            "sms send (interim logging adapter) to=%s template=%s",
            _mask_phone(to),
            template,
        )
