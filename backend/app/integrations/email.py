# backend/app/integrations/email.py
"""Email port: template-based sends with typed delivery records.

Design choice (recorded per task brief): `send` takes a template plus
variables — not subject/body — matching the frozen port shape in
docs/architecture/interfaces.md and the notification module's centralized
template rendering (Plan 07). Free-form subject/body composition would
scatter rendering across callers. Tests assert exact deliveries via
`FakeEmailSender.messages`.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


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
