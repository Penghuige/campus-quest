# backend/tests/unit/integrations/test_logging_senders.py
"""The logging SMS/EMAIL adapters' development semantics (PR #2
hardening P0-2, fail-closed).

Production never runs these adapters: config.py's production guard
refuses provider="logging" at Settings construction (pinned in
tests/unit/core/test_config.py). In development they stand in for real
providers, and the receipt they return — a "logging:"-prefixed id — is
what the DeliveryService records as provider_message_id, so a recorded
SENT delivery is distinguishable at a glance from a real provider send.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from app.integrations.email import LoggingEmailSender, build_email_sender
from app.integrations.sms import LoggingSmsSender, build_sms_sender


def test_logging_sms_sender_returns_prefixed_receipt() -> None:
    receipt = LoggingSmsSender().send(
        to="+8613800000000", template="otp_code", variables={"code": "123456"}
    )
    assert receipt is not None
    assert receipt.startswith("logging:")
    # The id after the prefix is a real UUID, unique per send like a
    # provider receipt.
    UUID(receipt.removeprefix("logging:"))


def test_logging_email_sender_returns_prefixed_receipt() -> None:
    receipt = LoggingEmailSender().send(
        to="student@campus.example.edu",
        template="email_verification",
        variables={"token": "t"},
    )
    assert receipt is not None
    assert receipt.startswith("logging:")
    UUID(receipt.removeprefix("logging:"))


def test_logging_receipts_are_unique_per_send() -> None:
    sender = LoggingSmsSender()
    first = sender.send(to="+8613800000000", template="t", variables={})
    second = sender.send(to="+8613800000000", template="t", variables={})
    assert first is not None and second is not None
    assert first != second


def test_build_senders_resolve_logging_provider() -> None:
    assert isinstance(build_sms_sender("logging"), LoggingSmsSender)
    assert isinstance(build_email_sender("logging"), LoggingEmailSender)


def test_build_senders_refuse_unknown_provider() -> None:
    # Defense in depth: pydantic already rejects values outside the
    # Literal at Settings load; a programmatic call with an unwired value
    # fails loudly instead of silently falling back to logging.
    with pytest.raises(LookupError, match="sms_provider"):
        build_sms_sender("twilio")
    with pytest.raises(LookupError, match="email_provider"):
        build_email_sender("smtp")
