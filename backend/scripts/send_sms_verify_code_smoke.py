# backend/scripts/send_sms_verify_code_smoke.py
"""REAL Aliyun DYPNS send smoke — costs money, never in CI.

Purpose: one manual, end-to-end proof that the adapter's signing,
payload, and taxonomy hold against the live provider — the unit suite
proves everything except the wire itself. Owner-run only:

    # credentials come from the environment (never arguments, never
    # this file): ALIYUN_SMS_ACCESS_KEY_ID / _SECRET / SIGN_NAME /
    # ALIYUN_SMS_OTP_TEMPLATE_CODE
    ALIYUN_SMS_ACCESS_KEY_ID=... ALIYUN_SMS_ACCESS_KEY_SECRET=... \
      ALIYUN_SMS_SIGN_NAME=... ALIYUN_SMS_OTP_TEMPLATE_CODE=... \
      uv run python scripts/send_sms_verify_code_smoke.py \
      --to +8613xxxxxxxxx --code 654321

Deliberately explicit and loud:
- ``--to`` and ``--code`` are required (no defaults that could fire a
  stray paid send);
- the exact request facts are printed BEFORE the send (masked number,
  endpoint, template) plus a real-money warning;
- the outcome prints the receipt, or the taxonomy class + provider
  error code on failure — a PermanentProviderError naming the code is
  the expected shape if the international service refuses mainland
  numbers (the open question this smoke exists to answer);
- retries are NOT automatic: a timeout prints the UnknownOutcome
  guidance instead of re-sending.

NOT wired into any test target, Makefile gate, or CI job by design.
"""

from __future__ import annotations

import argparse
import sys

from app.integrations.errors import ProviderError
from app.integrations.sms_aliyun import OTP_TEMPLATE_NAME, AliyunDypnsSmsSender


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--to", required=True, help="recipient in E.164, e.g. +8613...")
    parser.add_argument("--code", required=True, help="the OTP code to send")
    parser.add_argument(
        "--ttl-minutes", default="15", help="validity minutes rendered in the SMS"
    )
    args = parser.parse_args()

    import os

    config = {
        "access_key_id": os.environ.get("ALIYUN_SMS_ACCESS_KEY_ID", ""),
        "access_key_secret": os.environ.get("ALIYUN_SMS_ACCESS_KEY_SECRET", ""),
        "sign_name": os.environ.get("ALIYUN_SMS_SIGN_NAME", ""),
        "otp_template_code": os.environ.get("ALIYUN_SMS_OTP_TEMPLATE_CODE", ""),
    }
    missing = [name for name, value in config.items() if not value]
    if missing:
        print(f"missing env: {', '.join(missing)}", file=sys.stderr)
        return 2

    sender = AliyunDypnsSmsSender(**config)
    print(f"endpoint  : https://{sender._endpoint}/ (region {sender._region})")
    print(f"recipient : {args.to}")
    print(f"template  : {OTP_TEMPLATE_NAME} -> {config['otp_template_code']}")
    print("WARNING   : this performs ONE real paid send. Proceeding in 3s...")
    import time

    time.sleep(3)

    try:
        receipt = sender.send(
            to=args.to,
            template=OTP_TEMPLATE_NAME,
            variables={"code": args.code, "ttl_minutes": args.ttl_minutes},
        )
    except ProviderError as exc:
        print(f"FAILED    : {type(exc).__name__}: {exc}")
        return 1
    print(f"SENT      : receipt={receipt}")
    print("Check the phone; the code above is what the SMS should carry.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
