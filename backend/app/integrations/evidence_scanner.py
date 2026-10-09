"""Private evidence policies and scanner port, independent of Task parsers.

Type checks recognise signatures and reject obvious truncation. They are
not a complete document decoder or an assertion that a file is harmless.
Untrusted content is never rendered inline or executed by the API.
"""

from typing import Protocol

MAX_EVIDENCE_BYTES = 10 * 1024 * 1024
EVIDENCE_CONTENT_TYPES = frozenset({"application/pdf", "image/png", "image/jpeg"})


class InvalidEvidenceError(ValueError):
    """Permanent content/type/size rejection; message contains no file bytes."""


class EvidenceThreatError(ValueError):
    """Scanner found a threat, including a heuristic processing-limit alert."""


class EvidenceCheckUnavailableError(RuntimeError):
    """Unknown, transient or invalid scanner outcome; never a clean verdict."""


class EvidenceScanner(Protocol):
    def scan(self, content: bytes) -> None:
        """Return only after a complete clean scan, otherwise raise."""
        ...


def validate_evidence_type(content: bytes, declared_content_type: str) -> str:
    if not 0 < len(content) <= MAX_EVIDENCE_BYTES:
        raise InvalidEvidenceError("证明文件为空或超过大小限制")
    detected: str | None = None
    if content.startswith(b"%PDF-") and content.rstrip().endswith(b"%%EOF"):
        detected = "application/pdf"
    elif (
        content.startswith(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
        and len(content) >= 57
        and content.endswith(b"\x00\x00\x00\x00IEND\xaeB\x60\x82")
    ):
        detected = "image/png"
    elif content.startswith(b"\xff\xd8\xff") and content.endswith(b"\xff\xd9"):
        detected = "image/jpeg"
    if detected is None or detected != declared_content_type:
        raise InvalidEvidenceError("证明文件实际类型不匹配、已截断或不受支持")
    return detected
