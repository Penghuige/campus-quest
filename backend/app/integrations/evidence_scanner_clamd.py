"""Clamd INSTREAM with one wall-clock budget and fail-closed replies.

Deployment uses official databases, AlertExceedsMax and scan/stream caps
above the application's 10 MiB limit. No path-based command is used.
"""

import ipaddress
import re
import socket
import struct
import time

from app.core.config import Settings
from app.integrations.evidence_scanner import (
    MAX_EVIDENCE_BYTES,
    EvidenceCheckUnavailableError,
    EvidenceThreatError,
    InvalidEvidenceError,
)


class ClamdEvidenceScanner:
    def __init__(self, *, host: str, port: int = 3310, timeout: float = 30) -> None:
        # Literal private IP avoids an unbounded synchronous DNS lookup and
        # accidental use of unauthenticated clamd over the public internet.
        address = ipaddress.ip_address(host)
        if not (address.is_private or address.is_loopback) or address.is_unspecified:
            raise ValueError("Clamd requires a private literal IP address")
        if not 0 < timeout <= 30 or not 0 < port < 65536:
            raise ValueError("Invalid bounded Clamd endpoint policy")
        self._host = host
        self._family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
        self._port = port
        self._timeout = timeout

    @staticmethod
    def _remaining(sock: socket.socket, deadline: float) -> None:
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise EvidenceCheckUnavailableError("证明文件检查超时，请稍后重试")
        sock.settimeout(budget)

    def _request(
        self, command: bytes, deadline: float, content: bytes | None = None
    ) -> bytes:
        with socket.socket(self._family, socket.SOCK_STREAM) as sock:
            self._remaining(sock, deadline)
            sock.connect((self._host, self._port))
            self._remaining(sock, deadline)
            sock.sendall(command)
            if content is not None:
                for offset in range(0, len(content), 65536):
                    chunk = content[offset : offset + 65536]
                    self._remaining(sock, deadline)
                    sock.sendall(struct.pack(">I", len(chunk)) + chunk)
                self._remaining(sock, deadline)
                sock.sendall(b"\x00\x00\x00\x00")
            reply = bytearray()
            while True:
                self._remaining(sock, deadline)
                part = sock.recv(4097 - len(reply))
                if not part:
                    break
                reply.extend(part)
                if len(reply) > 4096:
                    raise EvidenceCheckUnavailableError("检查器响应无效")
            if not reply.endswith(b"\x00") or reply.count(0) != 1:
                raise EvidenceCheckUnavailableError("检查器响应不完整")
            return bytes(reply[:-1])

    def _capabilities(self, deadline: float) -> None:
        version = self._request(b"zVERSIONCOMMANDS\x00", deadline)
        match = re.fullmatch(
            rb"ClamAV [^/]+/([1-9][0-9]*)/[^|]+\| COMMANDS: (.+)", version
        )
        if match is None or b"INSTREAM" not in match.group(2).split():
            raise EvidenceCheckUnavailableError("检查器病毒库或扫描能力不可用")

    def scan(self, content: bytes) -> None:
        if not 0 < len(content) <= MAX_EVIDENCE_BYTES:
            raise InvalidEvidenceError("证明文件为空或超过大小限制")
        deadline = time.monotonic() + self._timeout
        try:
            self._capabilities(deadline)
            reply = self._request(b"zINSTREAM\x00", deadline, content)
        except OSError as exc:
            raise EvidenceCheckUnavailableError(
                "证明文件检查暂不可用，请稍后重试"
            ) from exc
        if reply == b"stream: OK":
            return
        if re.fullmatch(rb"stream: [\x20-\x7e]+ FOUND", reply):
            raise EvidenceThreatError("证明文件未通过安全检查")
        raise EvidenceCheckUnavailableError("证明文件检查未得到有效结果，请稍后重试")

    def check(self) -> bool:
        try:
            self._capabilities(time.monotonic() + self._timeout)
        except (OSError, EvidenceCheckUnavailableError):
            return False
        return True


def create_evidence_scanner(settings: Settings) -> ClamdEvidenceScanner:
    return ClamdEvidenceScanner(
        host=settings.clamd_host,
        port=settings.clamd_port,
        timeout=settings.clamd_timeout_seconds,
    )
