# backend/app/integrations/masking.py
"""Shared contact-info masking for logs (spec §5.4, §40; §15 discipline).

Phone numbers and emails are contact data: server logs may carry them only
masked, never in full (backend-engineering §15). Student numbers are
log-sensitive the same way — they ARE the students' login usernames (spec
§5.2), so a verbatim student number in a log line is a login identifier
leak. The exact forms live here once so every integration adapter — the
interim SMS/Email logging senders, the rate limiter's rejection line —
masks identically instead of growing per-module variants.
`app.modules.identity.otp` keeps its own private copy deliberately: the
domain layer does not import integration modules.
"""

from __future__ import annotations

# Below this length, first-2+last-2 would reveal most of the value (the
# spec §5.2 student-number minimum is 6 digits); nothing is kept.
_STUDENT_NUMBER_MIN_KEEP_LENGTH = 6


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


def mask_student_number(student_number: str) -> str:
    """Mask a student number: keep the first 2 and last 2 characters only.

    The mask is the FIXED four stars regardless of length — never scaled
    to the input — so the masked form never leaks the original's length.
    Values shorter than the spec §5.2 student-number minimum are masked
    fully (shape only), because keeping four of five characters would give
    the value away.
    """
    if len(student_number) < _STUDENT_NUMBER_MIN_KEEP_LENGTH:
        return "****"
    return f"{student_number[:2]}****{student_number[-2:]}"
