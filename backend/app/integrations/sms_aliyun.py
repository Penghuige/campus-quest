# backend/app/integrations/sms_aliyun.py
"""Real `SmsSender` adapter: Aliyun DYPNS ``SendSmsVerifyCode``.

The first production SMS channel (owner-approved 2026-10-05, local-stack
scope first). Two decisions shape this module:

**Caller-supplied code (not ``##code##``).** The API documents two ways
to fill the template's ``${code}``: the system-generated mode
(``{"code": "##code##"}``, verified via ``CheckSmsVerifyCode``) and the
caller-supplied mode (``{"code": "482913"}``, "阿里云接口无法校验").
We send OUR code: the OTP lifecycle — mint, HMAC-at-rest, attempt
counting, cooldown, rate limiting (spec §33.2) — stays entirely in
``identity.otp``; Aliyun is a delivery pipe. The system-generated mode
would move verification state into the provider and gut that contract.

**Hand-written ACS3 signature, no SDK.** The V3 scheme
(``ACS3-HMAC-SHA256``) is canonical-request + one HMAC over stdlib
primitives (``hmac``/``hashlib``) with an official worked example as
the golden vector (``test_acs3_signature.py`` proves the signer
byte-for-byte). The alternative — the ``alibabacloud-dypnsapi`` V2 SDK
family — drags the tea/credentials/openapi-util package chain in for
one endpoint call (backend-engineering §20: no new dependency when a
platform primitive suffices; httpx is already a runtime dependency).

Failure taxonomy (``integrations/errors``; G4/G5 — never fake success,
never swallow):

- HTTP 400 ``Code`` mapped per Aliyun's table: ``MOBILE_NUMBER_ILLEGAL``
  / ``INVALID_PARAMETERS`` / ``FUNCTION_NOT_OPENED`` /
  ``BUSINESS_NUMBER_ONE_DAY_LIMIT``-style day caps / signature-credential
  errors → ``PermanentProviderError``; ``FREQUENCY_FAIL`` /
  ``Throttling*`` → ``TemporaryProviderError``; unmapped 4xx codes are
  DEFINITE rejections → ``PermanentProviderError`` with the raw code
  preserved for diagnosis.
- Any 5xx → ``TemporaryProviderError``.
- ``ReadTimeout``/``WriteTimeout`` — the request MAY have reached the
  provider → ``UnknownOutcomeError`` (retry only with the idempotency
  key). ``ConnectError``/``ConnectTimeout`` never reached the wire →
  ``TemporaryProviderError``.

Credentials never enter any log line or exception message (the masking
discipline of ``sms.py``); only ``mask_phone``-ed recipients, template
names, receipts, and provider error codes are logged. ``RegionId`` is
deliberately NOT sent: it is not a ``SendSmsVerifyCode`` parameter and
an unknown signed parameter risks ``INVALID_PARAMETERS``; the region
stays a deployment fact (console/billing), not a wire argument.

The sender is SYNCHRONOUS like the port: notification delivery calls it
from Celery workers, and the one async caller (``identity.otp``) wraps
it in ``asyncio.to_thread`` — the ``finalize_upload`` precedent.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx
import phonenumbers

from app.core.clock import Clock, SystemClock
from app.integrations.errors import (
    PermanentProviderError,
    TemporaryProviderError,
    UnknownOutcomeError,
)
from app.integrations.masking import mask_phone

logger = logging.getLogger(__name__)

#: The one port template this adapter can deliver (the OTP flow's
#: constant, asserted equal from the test side). Any other template name
#: has no Aliyun template behind it — a permanent, loud rejection, not
#: a silent wrong-content send.
OTP_TEMPLATE_NAME = "phone_otp_verification"

_DEFAULT_ENDPOINT = "dypnsapi.aliyuncs.com"
_DEFAULT_REGION = "ap-southeast-1"
_API_VERSION = "2017-05-25"
_API_ACTION = "SendSmsVerifyCode"

#: Provider error codes that are definite, retry-cannot-help rejections
#: (invalid number/params, day-level caps, account or credential
#: problems). Day caps sit here on purpose: within the day the same
#: send cannot succeed, which is the taxonomy's definition of
#: permanent FOR THIS DELIVERY.
_PERMANENT_ERROR_CODES = frozenset(
    {
        "MOBILE_NUMBER_ILLEGAL",
        "INVALID_PARAMETERS",
        "FUNCTION_NOT_OPENED",
        "BUSINESS_LIMIT_CONTROL",
        "InvalidAccessKeyId.NotFound",
        "InvalidAccessKeyId",
        "SignatureDoesNotMatch",
        "SignatureNonceUsed",
        "InvalidTimeStamp.Expired",
        "AccessDenied",
        "Forbidden",
    }
)

#: Provider-side throttle/flow codes where a bounded retry can succeed.
_TEMPORARY_ERROR_CODES = frozenset(
    {
        "FREQUENCY_FAIL",
        "Throttling",
        "Throttling.User",
        "Throttling.User.Flow",
        "Throttling.Api",
        "InternalError",
        "ServiceUnavailable",
    }
)


def _percent_encode(value: str) -> str:
    """ACS3 percent-encoding: RFC 3986 unreserved only (space ``%20``,
    ``*`` ``%2A``) — exactly what ``quote`` with this safe set yields."""
    return quote(value, safe="-_.~")


def _canonical_query(params: Mapping[str, str]) -> str:
    return "&".join(
        f"{_percent_encode(name)}={_percent_encode(params[name])}"
        for name in sorted(params)
    )


def acs3_authorization(
    *,
    method: str,
    host: str,
    query: Mapping[str, str],
    body: bytes,
    access_key_id: str,
    access_key_secret: str,
    date: datetime,
    nonce: str,
    action: str = _API_ACTION,
    api_version: str = _API_VERSION,
) -> str:
    """Build the ``Authorization`` header for one Aliyun OpenAPI V3 call.

    Pure and stdlib-only: the canonical request is ``METHOD \\n / \\n
    canonical-query \\n canonical-headers \\n signed-headers \\n
    payload-hash``; the signature is lowercase-hex
    ``HMAC-SHA256(secret, "ACS3-HMAC-SHA256\\n" + hex(SHA256(request)))``.
    ``test_acs3_signature.py`` pins this against the official worked
    example (its own action/version), byte for byte.
    """
    payload_hash = hashlib.sha256(body).hexdigest()
    headers = {
        "host": host,
        "x-acs-action": action,
        "x-acs-content-sha256": payload_hash,
        "x-acs-date": date.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "x-acs-signature-nonce": nonce,
        "x-acs-version": api_version,
    }
    canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in headers)
    signed_headers = ";".join(headers)
    # canonical_headers already ends with "\n"; the join separator adds
    # the one blank line the spec's example shows between the header
    # block and SignedHeaders — no extra newline here.
    canonical_request = "\n".join(
        [
            method.upper(),
            "/",
            _canonical_query(query),
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )
    string_to_sign = (
        "ACS3-HMAC-SHA256\n" + hashlib.sha256(canonical_request.encode()).hexdigest()
    )
    digest = hmac.new(
        access_key_secret.encode(), string_to_sign.encode(), hashlib.sha256
    ).hexdigest()
    return (
        f"ACS3-HMAC-SHA256 Credential={access_key_id},"
        f"SignedHeaders={signed_headers},Signature={digest}"
    )


def _decompose_e164(to: str) -> tuple[str, str]:
    """Split an E.164 number into (CountryCode, national number).

    ``phonenumbers`` is already a runtime dependency (identity's login
    normalization) — the one reliable way to split an arbitrary E.164
    prefix. An unparseable recipient is rejected client-side with the
    same taxonomy the provider would answer (Permanent), before any
    signed request is spent on it.
    """
    try:
        parsed = phonenumbers.parse(to, region=None)
    except phonenumbers.NumberParseException as exc:
        # Masked recipient (spec §40 masking law): this message reaches
        # server logs through the 500 handler's traceback, so the
        # verbatim ``to`` — untrusted input that often embeds a full
        # phone — must not ride along (hardening D-4).
        raise PermanentProviderError(
            "SMS recipient is not a parseable E.164 number "
            f"(provider code equivalent MOBILE_NUMBER_ILLEGAL): {mask_phone(to)}"
        ) from exc
    return str(parsed.country_code), str(parsed.national_number)


class AliyunDypnsSmsSender:
    """`SmsSender` over Aliyun DYPNS ``SendSmsVerifyCode`` (V3 signed).

    ``transport`` is the test seam: production leaves it None (a real
    ``httpx.Client`` per send); unit tests inject ``httpx.MockTransport``
    and assert the exact signed request and taxonomy mapping without
    any wire.
    """

    def __init__(
        self,
        *,
        access_key_id: str,
        access_key_secret: str,
        sign_name: str,
        otp_template_code: str,
        endpoint: str = _DEFAULT_ENDPOINT,
        region: str = _DEFAULT_REGION,
        clock: Clock | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = 8.0,
    ) -> None:
        self._access_key_id = access_key_id
        self._access_key_secret = access_key_secret
        self._sign_name = sign_name
        self._otp_template_code = otp_template_code
        self._endpoint = endpoint
        # Not sent on the wire (see module docstring); a deployment fact
        # kept for diagnostics and the smoke script's banner.
        self._region = region
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._transport = transport
        self._timeout = timeout_seconds

    def send(
        self,
        *,
        to: str,
        template: str,
        variables: Mapping[str, Any],
        idempotency_key: str | None = None,
    ) -> str | None:
        if template != OTP_TEMPLATE_NAME:
            raise PermanentProviderError(
                f"no Aliyun template is mapped for SMS template {template!r} "
                f"(only {OTP_TEMPLATE_NAME!r} is deliverable)"
            )
        code = str(variables.get("code", ""))
        if not code:
            raise PermanentProviderError(
                "the OTP template requires a non-empty 'code' variable"
            )
        ttl_minutes = str(variables.get("ttl_minutes", "5"))
        country_code, phone_number = _decompose_e164(to)

        # Caller-supplied-code mode: ${code} is OUR code (module
        # docstring), ${min} renders the validity the user reads.
        template_param = json.dumps(
            {"code": code, "min": ttl_minutes},
            separators=(",", ":"),
            ensure_ascii=False,
        )
        params = {
            "CountryCode": country_code,
            "PhoneNumber": phone_number,
            "SignName": self._sign_name,
            "TemplateCode": self._otp_template_code,
            "TemplateParam": template_param,
        }
        if idempotency_key is not None:
            # Transparency only: the provider documents no dedupe
            # semantics for OutId, so UnknownOutcome retries still
            # rely on the PORT's idempotency-key discipline.
            params["OutId"] = idempotency_key

        now = self._clock.now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        # ONE nonce and ONE date build BOTH the signature and the sent
        # headers: the server verifies the signature against the wire
        # values, so any drift is a guaranteed SignatureDoesNotMatch.
        nonce = uuid.uuid4().hex
        date_header = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        payload_hash = hashlib.sha256(b"").hexdigest()
        authorization = acs3_authorization(
            method="POST",
            host=self._endpoint,
            query=params,
            body=b"",
            access_key_id=self._access_key_id,
            access_key_secret=self._access_key_secret,
            date=now,
            nonce=nonce,
        )
        # The query string is pre-encoded in the signer's canonical form
        # and handed to httpx verbatim: letting httpx re-encode `params`
        # could differ on sub-delims (`*`, `:`, `,` ride inside
        # TemplateParam) and desync the signed string from the sent one.
        request = httpx.Request(
            "POST",
            f"https://{self._endpoint}/?{_canonical_query(params)}",
            headers={
                "Authorization": authorization,
                "x-acs-action": _API_ACTION,
                "x-acs-version": _API_VERSION,
                "x-acs-date": date_header,
                "x-acs-signature-nonce": nonce,
                "x-acs-content-sha256": payload_hash,
            },
        )
        try:
            with httpx.Client(
                transport=self._transport, timeout=self._timeout
            ) as client:
                response = client.send(request)
        except httpx.TimeoutException as exc:
            if isinstance(exc, httpx.ConnectTimeout):
                raise TemporaryProviderError(
                    "Aliyun DYPNS unreachable before the request was "
                    "sent (connect timeout)"
                ) from exc
            raise UnknownOutcomeError(
                "Aliyun DYPNS send timed out after the request may have "
                "been dispatched; retry only under the idempotency-key "
                "discipline"
            ) from exc
        except httpx.TransportError as exc:
            raise TemporaryProviderError(
                f"Aliyun DYPNS transport failure: {type(exc).__name__}"
            ) from exc

        return self._interpret(response, to)

    def _interpret(self, response: httpx.Response, to: str) -> str | None:
        if response.status_code >= 500:
            raise TemporaryProviderError(
                f"Aliyun DYPNS server error HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise TemporaryProviderError(
                f"Aliyun DYPNS returned a non-JSON body (HTTP {response.status_code})"
            ) from exc
        provider_code = str(payload.get("Code") or "")
        if provider_code == "OK":
            model = payload.get("Model") or {}
            receipt = model.get("BizId") or model.get("RequestId")
            receipt_text = str(receipt) if receipt else None
            logger.info(
                "aliyun sms delivered to=%s template=%s receipt=%s",
                mask_phone(to),
                OTP_TEMPLATE_NAME,
                receipt_text,
            )
            return receipt_text
        if provider_code in _TEMPORARY_ERROR_CODES:
            raise TemporaryProviderError(
                f"Aliyun DYPNS rejected with retryable code {provider_code!r}"
            )
        if provider_code in _PERMANENT_ERROR_CODES:
            raise PermanentProviderError(
                f"Aliyun DYPNS rejected with terminal code {provider_code!r}"
            )
        # A definite rejection with an unmapped code: terminal for this
        # delivery (fail closed), raw code preserved for diagnosis.
        raise PermanentProviderError(
            f"Aliyun DYPNS rejected with unmapped code {provider_code!r} "
            f"(HTTP {response.status_code})"
        )
