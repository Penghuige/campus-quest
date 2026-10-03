# backend/tests/unit/identity/test_avatar_magic.py
"""Avatar magic-byte sniffing (QA defect #4, proposal D2).

Validation is by MAGIC BYTES, never by the client-declared Content-Type
or filename: both are client-controlled, so a mislabeled payload (an
SVG script, a PHP shell) must fail even while claiming image/png. No
imaging dependency — the header bytes alone identify the three accepted
formats (PNG signature / JPEG SOI / WebP RIFF container), per the
"no new dependency" rule.
"""

from __future__ import annotations

import pytest

from app.modules.identity.avatar_service import detect_avatar_type

# Canonical 67-byte 1x1 transparent PNG.
_PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6200010000050001"
    "0d0a2db40000000049454e44ae426082"
)
_JPEG_MIN = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x02\x00\x00\x01"
_WEBP_MIN = b"RIFF\x1a\x00\x00\x00WEBPVP8 \x0e\x00\x00\x00\x30\x01"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (_PNG_1PX, "png"),
        (_JPEG_MIN, "jpeg"),
        (_WEBP_MIN, "webp"),
    ],
)
def test_accepted_formats_detected(payload: bytes, expected: str) -> None:
    assert detect_avatar_type(payload) == expected


@pytest.mark.parametrize(
    "payload",
    [
        b"GIF89a\x01\x00\x01\x00\x00\xff\x00",  # gif: right magic, wrong format
        b"BM\x36\x00\x00\x00\x00\x00\x00\x00",  # bmp
        b"<svg xmlns='http://www.w3.org/2000/svg'/>",  # svg: XSS vector
        b"<?php echo file_get_contents('/etc/passwd'); ?>",
        b"\x89PNG",  # png magic but too short to be any real file
        b"\xff\xd8\xff",  # jpeg SOI only
        b"RIFF\x04\x00\x00\x00WAVE",  # RIFF but a WAV, not WEBP
        b"RIFF\x00\x00\x00\x00EBPX",  # WEBP marker at the wrong offset
        b"",
        b"\x00" * 64,  # all-NUL padding: no known signature
    ],
)
def test_rejected_payloads_return_none(payload: bytes) -> None:
    assert detect_avatar_type(payload) is None
