"""Fail-closed evidence checks through a real, bounded TCP protocol peer.

Mutations caught: accepting errors as clean, omitting stream termination,
trusting declared types, unbounded replies or per-recv timeout resets.
"""

import base64
import socket
import struct
import threading
import time
from contextlib import contextmanager, suppress

import pytest

VERSION = b"ClamAV 1.4.3/27700/Fri Oct 09 00:00:00 2026\x00"
COMMANDS = VERSION[:-1] + b"| COMMANDS: PING VERSION VERSIONCOMMANDS INSTREAM\x00"
PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aJXcAAAAASUVORK5CYII="
)


def receive(sock, size):
    result = b""
    while len(result) < size:
        part = sock.recv(size - len(result))
        if not part:
            raise AssertionError("Client truncated protocol request")
        result += part
    return result


@contextmanager
def daemon(reply=b"stream: OK\x00", *, version=COMMANDS, delay=0):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(2)
    port = listener.getsockname()[1]
    captured = []
    errors = []

    def serve():
        try:
            with listener.accept()[0] as conn:
                conn.settimeout(2)
                assert receive(conn, 17) == b"zVERSIONCOMMANDS\x00"
                conn.sendall(version)
            if b"INSTREAM" not in version or b"/27700/" not in version:
                return
            with listener.accept()[0] as conn:
                conn.settimeout(2)
                assert receive(conn, 10) == b"zINSTREAM\x00"
                body = bytearray()
                while True:
                    length = struct.unpack(">I", receive(conn, 4))[0]
                    if length == 0:
                        break
                    body.extend(receive(conn, length))
                captured.append(bytes(body))
                if delay:
                    time.sleep(delay)
                with suppress(BrokenPipeError, ConnectionResetError):
                    conn.sendall(reply)
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    try:
        yield port, captured
    finally:
        worker.join(timeout=3)
        listener.close()
        assert not worker.is_alive()
        assert not errors, errors


def test_complete_stream_and_explicit_clean_response():
    from app.integrations.evidence_scanner_clamd import ClamdEvidenceScanner

    content = b"bounded bytes" * 10000
    with daemon() as (port, captured):
        scanner = ClamdEvidenceScanner(host="127.0.0.1", port=port)
        assert scanner.scan(content) is None
        assert captured == [content]


@pytest.mark.parametrize(
    "reply",
    [
        b"stream: Virus FOUND\x00",
        b"stream: Heuristics.Limits.Exceeded FOUND\x00",
    ],
)
def test_detected_threat_is_never_clean(reply):
    from app.integrations.evidence_scanner import EvidenceThreatError
    from app.integrations.evidence_scanner_clamd import ClamdEvidenceScanner

    with daemon(reply) as (port, _), pytest.raises(EvidenceThreatError):
        ClamdEvidenceScanner(host="127.0.0.1", port=port).scan(PDF)


@pytest.mark.parametrize(
    "reply",
    [
        b"INSTREAM size limit exceeded. ERROR\x00",
        b"stream: unknown ERROR\x00",
        b"OK\x00",
        b"stream: OK",
        b"stream: OK\x00stream: ERROR\x00",
        b"stream: OK\n",
        b"x" * 5000 + b"\x00",
        b"\xff\x00",
        b"",
    ],
    ids=[
        "size-limit",
        "error",
        "ambiguous-ok",
        "unterminated",
        "multiple-records",
        "wrong-delimiter",
        "oversize-response",
        "non-ascii",
        "closed",
    ],
)
def test_protocol_failure_never_becomes_success(reply):
    from app.integrations.evidence_scanner import EvidenceCheckUnavailableError
    from app.integrations.evidence_scanner_clamd import ClamdEvidenceScanner

    with daemon(reply) as (port, _), pytest.raises(EvidenceCheckUnavailableError):
        ClamdEvidenceScanner(host="127.0.0.1", port=port).scan(PDF)


@pytest.mark.parametrize(
    "version",
    [VERSION, b"ClamAV 1.4.3/0/date| COMMANDS: INSTREAM\x00", b"UNKNOWN COMMAND\x00"],
)
def test_missing_database_or_capability_is_unavailable(version):
    from app.integrations.evidence_scanner import EvidenceCheckUnavailableError
    from app.integrations.evidence_scanner_clamd import ClamdEvidenceScanner

    with (
        daemon(version=version) as (port, _),
        pytest.raises(EvidenceCheckUnavailableError),
    ):
        ClamdEvidenceScanner(host="127.0.0.1", port=port).scan(PDF)


def test_scan_deadline_and_socket_failure_are_unavailable():
    from app.integrations.evidence_scanner import EvidenceCheckUnavailableError
    from app.integrations.evidence_scanner_clamd import ClamdEvidenceScanner

    with daemon(delay=0.3) as (port, _), pytest.raises(EvidenceCheckUnavailableError):
        ClamdEvidenceScanner(host="127.0.0.1", port=port, timeout=0.1).scan(PDF)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    with pytest.raises(EvidenceCheckUnavailableError):
        ClamdEvidenceScanner(host="127.0.0.1", port=port, timeout=0.1).scan(PDF)


@pytest.mark.parametrize(
    "content", [b"", b"x" * (10 * 1024 * 1024 + 1)], ids=["empty", "over-limit"]
)
def test_invalid_size_rejected_before_connect(content):
    from app.integrations.evidence_scanner import InvalidEvidenceError
    from app.integrations.evidence_scanner_clamd import ClamdEvidenceScanner

    with pytest.raises(InvalidEvidenceError):
        ClamdEvidenceScanner(host="127.0.0.1", port=1).scan(content)


@pytest.mark.parametrize(
    ("content", "mime"),
    [
        (PDF, "application/pdf"),
        (PNG, "image/png"),
        (b"\xff\xd8\xff\xe0jpeg\xff\xd9", "image/jpeg"),
    ],
)
def test_basic_signature_and_structure_match_declared_type(content, mime):
    from app.integrations.evidence_scanner import validate_evidence_type

    assert validate_evidence_type(content, mime) == mime


@pytest.mark.parametrize(
    ("content", "mime"),
    [
        (b"MZ executable", "application/pdf"),
        (PNG, "application/pdf"),
        (b"%PDF-1.7\ntruncated", "application/pdf"),
        (PNG[:-12], "image/png"),
        (b"\xff\xd8\xfftruncated", "image/jpeg"),
        (PDF, "application/zip"),
    ],
)
def test_disguised_unsupported_and_truncated_content_rejected(content, mime):
    from app.integrations.evidence_scanner import (
        InvalidEvidenceError,
        validate_evidence_type,
    )

    with pytest.raises(InvalidEvidenceError):
        validate_evidence_type(content, mime)


def configured_settings(**overrides):
    from app.core.config import Settings

    return Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://test:test@localhost/campusquest_test",
        redis_url="redis://localhost",
        s3_endpoint_url="http://localhost",
        s3_bucket="test",
        s3_access_key="test",
        s3_secret_key="test",
        business_timezone="Asia/Shanghai",
        environment="development",
        **overrides,
    )


def test_settings_factory_uses_real_configured_endpoint():
    from app.integrations.evidence_scanner_clamd import create_evidence_scanner

    with daemon() as (port, captured):
        settings = configured_settings(clamd_host="127.0.0.1", clamd_port=port)
        assert create_evidence_scanner(settings).scan(PDF) is None
        assert captured == [PDF]


@pytest.mark.parametrize(
    "settings",
    [
        {"clamd_host": "8.8.8.8"},
        {"clamd_host": "scanner.example.com"},
        {"clamd_host": "0.0.0.0"},
        {"clamd_port": 0},
        {"clamd_port": 65536},
        {"clamd_timeout_seconds": 0},
        {"clamd_timeout_seconds": 31},
    ],
)
def test_invalid_scanner_configuration_rejected(settings):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        configured_settings(**settings)
