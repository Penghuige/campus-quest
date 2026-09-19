# backend/app/integrations/masking.py
"""Shared contact-info masking for logs (spec §5.4, §40; §15 discipline).

Phone numbers and emails are contact data: server logs may carry them only
masked, never in full (backend-engineering §15). The exact forms live here
once so every integration adapter — the interim SMS/Email logging senders,
the rate limiter's rejection line — masks identically instead of growing
per-module variants. `app.modules.identity.otp` keeps its own private copy
deliberately: the domain layer does not import integration modules.
"""

from __future__ import annotations


def mask_phone(phone_e164: str) -> str:
    """Mask an E.164 phone: keep the prefix and the last 4 digits."""
    if len(phone_e164) <= 8:
        return f"{phone_e164[:2]}****"
    return f"{phone_e164[:3]}****{phone_e164[-4:]}"


def mask_email(email: str) -> str:
    """Mask an email: first local character + domain only."""
    local, separator, domain = email.partition("@")
    if not separator or not local:
        return "***"
    return f"{local[:1]}***@{domain}"
