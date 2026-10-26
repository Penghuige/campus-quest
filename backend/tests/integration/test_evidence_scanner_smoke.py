"""Opt-in real official-database Clamd smoke; never substitute a fake engine."""

import os
from io import BytesIO
from zipfile import ZIP_STORED, ZipFile

import pytest

from app.integrations.evidence_scanner import EvidenceThreatError
from app.integrations.evidence_scanner_clamd import ClamdEvidenceScanner

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("CQ_EVIDENCE_SCAN_SMOKE") != "1",
        reason="Requires official-database Clamd (CQ_EVIDENCE_SCAN_SMOKE=1)",
    ),
]

# Standard harmless anti-virus test marker, not a real malicious program.
EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


def scanner():
    return ClamdEvidenceScanner(
        host=os.environ.get("CLAMD_HOST", "127.0.0.1"),
        port=int(os.environ.get("CLAMD_PORT", "3310")),
    )


def test_real_clamd_ready_and_clean_file():
    adapter = scanner()
    assert adapter.check()
    assert adapter.scan(b"A small clean evidence document.") is None


def test_real_clamd_detects_eicar():
    with pytest.raises(EvidenceThreatError):
        scanner().scan(EICAR)


def test_real_clamd_scans_entire_10_mib_including_final_threat():
    # EICAR is recognised as a standalone 68-byte file, not as a marker
    # arbitrarily appended to unrelated bytes. Put that real file last in
    # a stored archive to prove the engine processes the far boundary.
    # API evidence types still exclude archives; this probes the scanner.
    def archive(padding):
        output = BytesIO()
        with ZipFile(output, "w", compression=ZIP_STORED) as files:
            files.writestr("padding.txt", b" " * padding)
            files.writestr("eicar.txt", EICAR)
        return output.getvalue()

    content = archive(10 * 1024 * 1024 - len(archive(0)))
    assert len(content) == 10485760
    with pytest.raises(EvidenceThreatError):
        scanner().scan(content)
