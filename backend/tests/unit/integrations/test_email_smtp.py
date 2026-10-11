# backend/tests/unit/integrations/test_email_smtp.py
"""The SMTP EmailSender adapter's contract matrix (security round
batch 4 item 4).

smtplib is stubbed at the class seam (the adapter's only touch point
is ``smtplib.SMTP``): the fake client records the message and the
session calls (starttls/login/send_message) and raises the programmed
exception, so the §13 classification matrix — temporary / permanent /
unknown — is asserted against the REAL exception types the stdlib
produces, exactly the golden-vector discipline the Aliyun SMS adapter
tests use.

The factory and production-guard tests live with the config suite's
provider tests; the ones here pin the composition-time refusals.
"""

from __future__ import annotations

import smtplib
from types import SimpleNamespace
from typing import Any

import pytest

from app.integrations.email import build_email_sender
from app.integrations.email_smtp import SmtpEmailSender
from app.integrations.errors import (
    PermanentProviderError,
    TemporaryProviderError,
    UnknownOutcomeError,
)


class FakeSMTP:
    """Programmable smtplib.SMTP stand-in (context manager, session
    calls recorded, one programmed failure)."""

    last: FakeSMTP | None = None

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.calls: list[str] = []
        self.sent: list[Any] = []
        self.failure: Exception | None = FakeSMTP._failure
        FakeSMTP.last = self

    _failure: Exception | None = None

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.calls.append("closed")

    def starttls(self) -> None:
        self.calls.append("starttls")

    def login(self, user: str, password: str) -> None:
        self.calls.append(f"login:{user}:{password}")

    def send_message(self, message: Any) -> None:
        if self.failure is not None:
            raise self.failure
        self.sent.append(message)


def _sender(**overrides: Any) -> SmtpEmailSender:
    fields = {
        "host": "smtp.example.edu.cn",
        "port": 587,
        "username": "campus@example.edu.cn",
        "password": "relay-pass",
        "from_address": "campus@example.edu.cn",
        "use_tls": True,
    }
    fields.update(overrides)
    return SmtpEmailSender(**fields)


@pytest.fixture
def stub_smtp(monkeypatch: pytest.MonkeyPatch) -> type[FakeSMTP]:
    FakeSMTP._failure = None
    FakeSMTP.last = None
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


def test_notification_shape_sends_rendered_title_and_body(
    stub_smtp: type[FakeSMTP],
) -> None:
    receipt = _sender().send(
        to="student@example.edu.cn",
        template="deadline_24h",
        variables={"title": "任务截止提醒", "body": "您的任务将在 24 小时后截止。"},
        idempotency_key="k1",
    )
    assert receipt is None  # stdlib surfaces no provider id (port allows None)
    client = stub_smtp.last
    assert client is not None
    assert client.calls == [
        "starttls",
        "login:campus@example.edu.cn:relay-pass",
        "closed",
    ]
    message = client.sent[0]
    assert message["Subject"] == "任务截止提醒"
    assert message["From"] == "CampusQuest <campus@example.edu.cn>"
    assert message["To"] == "student@example.edu.cn"
    assert "24 小时后截止" in message.get_content()


def test_verification_shape_composes_the_token_email(
    stub_smtp: type[FakeSMTP],
) -> None:
    _sender().send(
        to="student@example.edu.cn",
        template="email_verification",
        variables={"token": "tok-123", "ttl_hours": "24"},
    )
    message = stub_smtp.last.sent[0]
    assert message["Subject"] == "CampusQuest 邮箱验证"
    assert "tok-123" in message.get_content()
    assert "24" in message.get_content()


def test_unknown_template_shape_is_permanent(stub_smtp: type[FakeSMTP]) -> None:
    with pytest.raises(PermanentProviderError):
        _sender().send(to="x@example.edu.cn", template="mystery", variables={"a": "b"})
    assert stub_smtp.last is None  # refused before any network work


@pytest.mark.parametrize(
    "failure,expected",
    [
        # Definite rejections — retry cannot change the answer.
        (
            smtplib.SMTPAuthenticationError(535, b"bad credentials"),
            PermanentProviderError,
        ),
        (
            smtplib.SMTPRecipientsRefused({"x@example.edu.cn": (550, b"no")}),
            PermanentProviderError,
        ),
        # Transient — bounded retry safe.
        (smtplib.SMTPResponseException(421, b"try later"), TemporaryProviderError),
        (smtplib.SMTPServerDisconnected("dropped"), TemporaryProviderError),
        (OSError("network down"), TemporaryProviderError),
        # The message may have left — idempotency-key retry only.
        (TimeoutError("timed out"), UnknownOutcomeError),
        # A 5xx general response is a definite rejection.
        (smtplib.SMTPResponseException(550, b"rejected"), PermanentProviderError),
    ],
)
def test_error_classification_matrix(
    stub_smtp: type[FakeSMTP],
    failure: Exception,
    expected: type[Exception],
) -> None:
    stub_smtp._failure = failure
    with pytest.raises(expected):
        _sender().send(
            to="student@example.edu.cn",
            template="deadline_24h",
            variables={"title": "t", "body": "b"},
        )


def test_factory_refuses_smtp_without_settings() -> None:
    with pytest.raises(LookupError, match="needs the deployment settings"):
        build_email_sender("smtp")


def test_factory_refuses_smtp_with_incomplete_settings() -> None:
    settings = SimpleNamespace(
        smtp_host="",
        smtp_port=587,
        smtp_username="u",
        smtp_password="p",
        smtp_from_address="",
        smtp_use_tls=True,
    )
    with pytest.raises(LookupError, match="smtp_host, smtp_from_address"):
        build_email_sender("smtp", settings)


def test_factory_builds_smtp_sender_from_complete_settings() -> None:
    settings = SimpleNamespace(
        smtp_host="smtp.example.edu.cn",
        smtp_port=465,
        smtp_username="u",
        smtp_password="p",
        smtp_from_address="campus@example.edu.cn",
        smtp_use_tls=False,
    )
    sender = build_email_sender("smtp", settings)
    assert isinstance(sender, SmtpEmailSender)
    assert sender._use_tls is False
