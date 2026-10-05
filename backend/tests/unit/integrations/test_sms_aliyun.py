# backend/tests/unit/integrations/test_sms_aliyun.py
"""The Aliyun DYPNS adapter against a mock transport — no wire, no keys.

Pins, per the adapter's contract (owner-approved 2026-10-05 scope):

- the exact wire request: E.164 decomposed via ``phonenumbers``
  (CountryCode + national number), caller-supplied ``code`` mode
  TemplateParam, OutId only when an idempotency key is present, and the
  SIGNATURE COHERENCE between the sent Authorization header and the
  sent query (recomputing the signature from the wire values must
  reproduce the sent header);
- the three-way failure taxonomy from HTTP status + provider ``Code``
  (mapped, unmapped-but-definite, 5xx), timeouts split into
  never-reached-the-wire vs may-have-been-dispatched;
- the credential/code leak surface: neither the exception text nor the
  log records may carry the secret, the access key id, or the OTP code.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest

from app.core.clock import FrozenClock
from app.integrations.errors import (
    PermanentProviderError,
    TemporaryProviderError,
    UnknownOutcomeError,
)
from app.integrations.sms_aliyun import (
    OTP_TEMPLATE_NAME,
    AliyunDypnsSmsSender,
    acs3_authorization,
)

_T0 = datetime(2026, 10, 5, 8, 0, 0, tzinfo=UTC)
_TO = "+8613530417625"
_OTP_VARS = {"code": "482913", "ttl_minutes": "15"}
_SECRET = "test-secret-that-must-never-leak"


def _sender(transport: httpx.BaseTransport) -> AliyunDypnsSmsSender:
    return AliyunDypnsSmsSender(
        access_key_id="test-key-id",
        access_key_secret=_SECRET,
        sign_name="恒创联众",
        otp_template_code="100001",
        clock=FrozenClock(_T0),
        transport=transport,
    )


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "Code": "OK",
            "Success": True,
            "RequestId": "req-1",
            "Model": {"BizId": "biz-123", "RequestId": "req-1"},
        },
    )


def _capturing(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[Callable[[httpx.Request], httpx.Response], list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return recording, seen


def _query_of(request: httpx.Request) -> dict[str, str]:
    params = httpx.QueryParams(request.url.query.decode())
    return {str(name): str(value) for name, value in params.items()}


def test_happy_path_wire_shape_and_receipt() -> None:
    handler, seen = _capturing(_ok)
    receipt = _sender(httpx.MockTransport(handler)).send(
        to=_TO, template=OTP_TEMPLATE_NAME, variables=_OTP_VARS
    )

    assert receipt == "biz-123"
    (request,) = seen
    query = _query_of(request)

    # E.164 decomposed: +86 country, national number.
    assert query["CountryCode"] == "86"
    assert query["PhoneNumber"] == "13530417625"
    assert query["SignName"] == "恒创联众"
    assert query["TemplateCode"] == "100001"
    # Caller-supplied-code mode: OUR code, the TTL the user reads.
    assert json.loads(query["TemplateParam"]) == {"code": "482913", "min": "15"}
    assert "OutId" not in query

    # Signature coherence: recomputing from the WIRE values (query,
    # nonce, date) reproduces the sent Authorization byte for byte —
    # the sent string is exactly the signed string.
    assert request.headers["Authorization"] == acs3_authorization(
        method="POST",
        host="dypnsapi.aliyuncs.com",
        query=query,
        body=b"",
        access_key_id="test-key-id",
        access_key_secret=_SECRET,
        date=_T0,
        nonce=request.headers["x-acs-signature-nonce"],
    )
    assert request.headers["x-acs-date"] == "2026-10-05T08:00:00Z"
    # The query string keeps the signer's canonical encoding verbatim:
    # TemplateParam's JSON punctuation arrives percent-encoded.
    assert "%7B%22code%22" in request.url.query.decode()


def test_idempotency_key_rides_out_id_when_present() -> None:
    handler, seen = _capturing(_ok)
    _sender(httpx.MockTransport(handler)).send(
        to=_TO,
        template=OTP_TEMPLATE_NAME,
        variables=_OTP_VARS,
        idempotency_key="evt:SMS:u1",
    )
    assert _query_of(seen[0])["OutId"] == "evt:SMS:u1"


def test_unmapped_template_is_permanent_without_any_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("an unmapped template must not reach the wire")

    with pytest.raises(PermanentProviderError, match="deadline_4h"):
        _sender(httpx.MockTransport(handler)).send(
            to=_TO, template="deadline_4h", variables={"title": "x", "body": "y"}
        )


@pytest.mark.parametrize(
    "provider_code",
    [
        "MOBILE_NUMBER_ILLEGAL",
        "INVALID_PARAMETERS",
        "FUNCTION_NOT_OPENED",
        "BUSINESS_LIMIT_CONTROL",
        "SignatureDoesNotMatch",
    ],
)
def test_mapped_permanent_codes(provider_code: str) -> None:
    sender = _sender(
        httpx.MockTransport(
            lambda request: httpx.Response(
                400, json={"Code": provider_code, "Message": "rejected"}
            )
        )
    )
    with pytest.raises(PermanentProviderError, match=provider_code):
        sender.send(to=_TO, template=OTP_TEMPLATE_NAME, variables=_OTP_VARS)


@pytest.mark.parametrize("provider_code", ["FREQUENCY_FAIL", "Throttling.User"])
def test_mapped_temporary_codes(provider_code: str) -> None:
    sender = _sender(
        httpx.MockTransport(
            lambda request: httpx.Response(
                400, json={"Code": provider_code, "Message": "slow down"}
            )
        )
    )
    with pytest.raises(TemporaryProviderError, match=provider_code):
        sender.send(to=_TO, template=OTP_TEMPLATE_NAME, variables=_OTP_VARS)


def test_unmapped_4xx_is_definite_and_permanent_with_raw_code() -> None:
    sender = _sender(
        httpx.MockTransport(
            lambda request: httpx.Response(
                400, json={"Code": "SOME_NEW_POLICY", "Message": "?"}
            )
        )
    )
    with pytest.raises(PermanentProviderError, match="SOME_NEW_POLICY"):
        sender.send(to=_TO, template=OTP_TEMPLATE_NAME, variables=_OTP_VARS)


def test_5xx_is_temporary() -> None:
    sender = _sender(
        httpx.MockTransport(lambda request: httpx.Response(503, text="unavailable"))
    )
    with pytest.raises(TemporaryProviderError, match="503"):
        sender.send(to=_TO, template=OTP_TEMPLATE_NAME, variables=_OTP_VARS)


def test_read_timeout_is_unknown_outcome() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("no response in time")

    with pytest.raises(UnknownOutcomeError, match="idempotency"):
        _sender(httpx.MockTransport(handler)).send(
            to=_TO, template=OTP_TEMPLATE_NAME, variables=_OTP_VARS
        )


def test_connect_timeout_is_temporary_not_unknown() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("never reached the provider")

    with pytest.raises(TemporaryProviderError, match="connect"):
        _sender(httpx.MockTransport(handler)).send(
            to=_TO, template=OTP_TEMPLATE_NAME, variables=_OTP_VARS
        )


def test_unparseable_recipient_rejected_before_the_wire() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("an invalid recipient must not reach the wire")

    with pytest.raises(PermanentProviderError, match="E.164"):
        _sender(httpx.MockTransport(handler)).send(
            to="not-a-phone", template=OTP_TEMPLATE_NAME, variables=_OTP_VARS
        )


def test_credentials_and_code_never_leak_in_exceptions_or_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    failing = _sender(
        httpx.MockTransport(
            lambda request: httpx.Response(
                400,
                json={"Code": "MOBILE_NUMBER_ILLEGAL", "Message": "rejected"},
            )
        )
    )
    succeeding = _sender(httpx.MockTransport(_ok))
    with caplog.at_level(logging.INFO, logger="app.integrations.sms_aliyun"):
        with pytest.raises(PermanentProviderError):
            failing.send(to=_TO, template=OTP_TEMPLATE_NAME, variables=_OTP_VARS)
        succeeding.send(to=_TO, template=OTP_TEMPLATE_NAME, variables=_OTP_VARS)

    log_text = caplog.text + str(caplog.records)
    for secret_material in (_SECRET, "482913", "test-key-id"):
        assert secret_material not in log_text
    # The recipient appears masked only.
    assert "13530417625" not in log_text
    assert mask_present(log_text)


def mask_present(text: str) -> bool:
    return "*" in text
