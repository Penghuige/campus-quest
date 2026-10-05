# backend/tests/unit/integrations/test_acs3_signature.py
"""The ACS3 signer against Aliyun's official worked example.

The golden vector is the complete step-by-step example in the V3
signature documentation (help.aliyun.com, "V3 请求结构和签名"): fixed
credentials, host, action, date, nonce, and query string produce the
published final ``Authorization`` header. A pass here means our
hand-written signer (the no-SDK decision in ``sms_aliyun``) matches the
provider's verifier on the exact bytes, not merely on shape.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.integrations.sms_aliyun import acs3_authorization

# The documentation example's fixed inputs.
_DATE = datetime(2023, 10, 26, 10, 22, 32, tzinfo=UTC)
_NONCE = "3156853299f313e23d1673dc12e1703d"
_QUERY = {
    "ImageId": "win2019_1809_x64_dtc_zh-cn_40G_alibase_20230811.vhd",
    "RegionId": "cn-shanghai",
}
_EXPECTED_AUTHORIZATION = (
    "ACS3-HMAC-SHA256 Credential=YourAccessKeyId,"
    "SignedHeaders=host;x-acs-action;x-acs-content-sha256;x-acs-date;"
    "x-acs-signature-nonce;x-acs-version,"
    "Signature=06563a9e1b43f5dfe96b81484da74bceab24a1d853912eee15083a6f0f3283c0"
)


def test_golden_example_reproduces_the_published_signature() -> None:
    authorization = acs3_authorization(
        method="POST",
        host="ecs.cn-shanghai.aliyuncs.com",
        query=_QUERY,
        body=b"",
        access_key_id="YourAccessKeyId",
        access_key_secret="YourAccessKeySecret",
        date=_DATE,
        nonce=_NONCE,
        action="RunInstances",
        api_version="2014-05-26",
    )
    assert authorization == _EXPECTED_AUTHORIZATION
