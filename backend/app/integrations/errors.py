# backend/app/integrations/errors.py
"""Adapter failure taxonomy for external systems.

Real adapters translate provider SDK exceptions into exactly one of these
categories so callers can pick a retry policy
(docs/quality/backend-engineering.md §13):

- temporary failure -> `TemporaryProviderError`: bounded retry is safe;
- permanent provider rejection -> `PermanentProviderError`: retry cannot
  succeed; surface as terminal for this delivery/operation;
- timeout or unknown outcome -> `UnknownOutcomeError`: the side effect may
  have happened; retry only with the provider idempotency key;
- already-exists / idempotent response: no V1 port operation needs a
  distinct exception; adapters surface it as a normal return value (for
  example `ObjectStorage.head_object` returning `None` for a missing
  object). Introduce a typed `AlreadyExistsError` here only when a port
  gains a mutating operation that can collide.

Fakes raise the same taxonomy when programmed to fail, so worker retry
tests exercise the same branches production adapters produce.
"""


class ProviderError(Exception):
    """Base class for adapter-translated external-provider failures."""


class TemporaryProviderError(ProviderError):
    """Transient provider failure; bounded retry is safe."""


class PermanentProviderError(ProviderError):
    """Provider definitively rejected the request; retry cannot succeed."""


class UnknownOutcomeError(ProviderError):
    """Timeout or otherwise unknown outcome.

    The side effect may have happened; callers retry only with the
    provider idempotency key to avoid duplicating the effect.
    """
