# backend/app/integrations/email_smtp.py
"""Real `EmailSender` adapter over stdlib smtplib (security round
batch 4 item 4; zero new dependencies).

The port's wire contract carries finished text in ``variables`` for
the notification channel (``title``/``body`` — delivery renders
NOTHING at dispatch, spec §25) and structured inputs for the identity
verification email (``token``/``ttl_hours``); the adapter owns the
provider-side presentation for the latter exactly like the SMS
adapter owns its sign name.

§13 error taxonomy, mirroring ``sms_aliyun``:

- connect/disconnect/socket errors → ``TemporaryProviderError``
  (bounded retry safe);
- SMTP 4xx → ``TemporaryProviderError``; 5xx → ``PermanentProviderError``
  (auth refused, sender refused, all-recipients-refused are explicit
  permanents — a retry cannot fix a wrong credential or a rejected
  address);
- socket timeout AFTER the message may have left → ``UnknownOutcomeError``
  (retry only with the idempotency key, exactly the SMS contract).

Logging records the masked recipient, subject, and template id only —
NEVER ``variables`` (the verification token travels there) and never
credentials. The stdlib SMTP client surfaces no receipt id, so
successful sends return ``None`` (the port documents that shape).

``smtplib`` is synchronous: callers on the event loop must ride
``asyncio.to_thread`` (the identity flow does; the notification worker
runs each send inside its own ``asyncio.run`` task).
"""

from __future__ import annotations

import logging
import smtplib
from collections.abc import Mapping
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any

from app.integrations.errors import (
    PermanentProviderError,
    TemporaryProviderError,
    UnknownOutcomeError,
)
from app.integrations.masking import mask_email

logger = logging.getLogger(__name__)

#: The identity verification email's subject (provider-side
#: presentation, like the SMS adapter's sign name).
_VERIFICATION_SUBJECT = "CampusQuest 邮箱验证"

#: The staff login-code email (V1.1 §2.1): the code and its TTL are the
#: whole message — no links, no account hints (anti-enumeration).
_LOGIN_CODE_SUBJECT = "CampusQuest 登录验证码"


def _login_code_body(variables: Mapping[str, Any]) -> str:
    code = variables.get("code", "")
    ttl_minutes = variables.get("ttl_minutes", "")
    return (
        "您正在登录 CampusQuest。\n\n"
        f"登录验证码：{code}\n\n"
        f"验证码 {ttl_minutes} 分钟内有效；如非本人操作，请忽略本邮件。\n"
        "\n—— CampusQuest"
    )


def _verification_body(variables: Mapping[str, Any]) -> str:
    token = variables.get("token", "")
    ttl_hours = variables.get("ttl_hours", "")
    return (
        "您正在验证 CampusQuest 账号邮箱。\n\n"
        f"验证码/令牌：{token}\n\n"
        f"令牌 {ttl_hours} 小时内有效；如非本人操作请忽略本邮件。\n"
        "\n—— CampusQuest"
    )


class SmtpEmailSender:
    """`EmailSender` over a real SMTP relay (stdlib smtplib).

    ``host``/``port``/``username``/``password``/``from_address`` come
    from the ``SMTP_*`` settings group; ``use_tls`` picks STARTTLS
    (the submission-port posture, default) over plaintext. The factory
    refuses an ``smtp`` selection with any required field empty in
    EVERY environment — same fail-closed shape as the SMS adapter.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        from_address: str,
        use_tls: bool = True,
        timeout_seconds: float = 8.0,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._from_address = from_address
        self._use_tls = use_tls
        self._timeout = timeout_seconds

    def send(
        self,
        *,
        to: str,
        template: str,
        variables: Mapping[str, Any],
        idempotency_key: str | None = None,
    ) -> str | None:
        """Send one templated email; see the port for the contract.

        Returns ``None`` on success — the stdlib client surfaces no
        provider receipt id, and the port documents that adapters
        without a receipt keep it ``None``.
        """
        if "title" in variables and "body" in variables:
            # The notification channel: finished text, rendered at
            # record time (delivery_service — nothing renders here).
            subject = str(variables["title"])
            body = str(variables["body"])
        elif template == "email_verification":
            subject = _VERIFICATION_SUBJECT
            body = _verification_body(variables)
        elif template == "staff_login_code":
            subject = _LOGIN_CODE_SUBJECT
            body = _login_code_body(variables)
        else:
            raise PermanentProviderError(
                f"unknown email template shape {template!r} without "
                "title/body variables"
            )

        message = EmailMessage()
        message["From"] = formataddr(("CampusQuest", self._from_address))
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body, subtype="plain", charset="utf-8")

        try:
            with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as client:
                if self._use_tls:
                    client.starttls()
                client.login(self._username, self._password)
                client.send_message(message)
        except (
            smtplib.SMTPAuthenticationError,
            smtplib.SMTPSenderRefused,
            smtplib.SMTPRecipientsRefused,
        ) as exc:
            # Definite rejections: a wrong credential, a refused
            # sender, or every recipient refused — retrying cannot
            # change the answer.
            logger.warning(
                "smtp email permanently rejected to=%s template=%s smtp_code=%s",
                mask_email(to),
                template,
                _smtp_code(exc),
            )
            raise PermanentProviderError(f"smtp rejected the send: {exc}") from exc
        except smtplib.SMTPResponseException as exc:
            code = int(exc.smtp_code)
            logger.warning(
                "smtp email %s to=%s template=%s smtp_code=%s",
                "temporarily refused" if code < 500 else "rejected",
                mask_email(to),
                template,
                code,
            )
            if code < 500:
                detail = (
                    exc.smtp_error.decode(errors="replace")
                    if isinstance(exc.smtp_error, bytes)
                    else str(exc.smtp_error)
                )
                raise TemporaryProviderError(f"smtp {code}: {detail}") from exc
            raise PermanentProviderError(f"smtp {code} rejected the send") from exc
        except TimeoutError as exc:
            # The message may already have left: unknown outcome, retry
            # only with the idempotency key (the §25.3 contract).
            logger.warning(
                "smtp email outcome unknown to=%s template=%s",
                mask_email(to),
                template,
            )
            raise UnknownOutcomeError(f"smtp timed out: {exc}") from exc
        except (
            smtplib.SMTPConnectError,
            smtplib.SMTPServerDisconnected,
            OSError,
        ) as exc:
            logger.warning(
                "smtp email temporarily failed to=%s template=%s",
                mask_email(to),
                template,
            )
            raise TemporaryProviderError(f"smtp unreachable: {exc}") from exc

        logger.info(
            "smtp email sent to=%s subject=%s template=%s idempotency_key=%s",
            mask_email(to),
            subject,
            template,
            idempotency_key,
        )
        return None


def _smtp_code(exc: Exception) -> str:
    code = getattr(exc, "smtp_code", None)
    return str(code) if code is not None else "-"
