# backend/app/modules/submissions/validation_runner.py
"""Sandboxed execution of the format validators (spec §33.3 解析器隔离:
CPU/内存/时间限制; plan 04 task 7; the parked rulings of tasks 4-6).

The controller decision implemented here: a validator NEVER runs in
the worker process. It runs in a throwaway SUBPROCESS that

- is killed at a hard WALL-CLOCK deadline (``subprocess.run(timeout=)``
  -> SIGKILL), closing the cooperative-checkpoint gaps of task 4
  (slow-trickling I/O never reached a row checkpoint) and the
  pre-row blind spots of tasks 5/6;
- has its ADDRESS SPACE capped via ``RLIMIT_AS`` set in the child
  before exec, killing the ~2.5 GB chartsheet amplification parked in
  task 5 even though it hides in exempt archive families;
- has its CPU time capped via ``RLIMIT_CPU`` (SIGXCPU at the soft
  limit, SIGKILL at the hard — both set equal).

Failure taxonomy (backend-engineering §14: resource-limit failures
must become terminal validation outcomes, never a hung or crashing
worker pool):

- deadline exceeded -> a synthetic report carrying the stable code
  ``VALIDATION_TIMED_OUT``;
- child exit != 0, unparseable stdout, or an OOM kill -> the stable
  code ``VALIDATION_WORKER_CRASHED``. The child's stderr tail is kept
  on the outcome for SERVER-SIDE logging only — it never enters the
  persisted report (no parser internals to students, spec §12.4).

Child protocol (``validators.worker_entry``): argv[1] is the temp file
path; stdin carries the JSON request (raw schema, preview spec,
limits); stdout carries exactly one JSON object
``{"detected_type": ..., "report": ...}`` where ``detected_type`` is
``null`` when the content matched none of the three types (detection
runs INSIDE the child so a hostile archive dies against the child's
limits, not the worker's). The child imports only the validator
modules — no database, no services. An ``OSError`` escaping VALIDATOR
EXECUTION is converted INSIDE the child into a structured
``MALFORMED_*`` report (exit 0): by then the file is a local temp the
parent already downloaded, so an I/O-level parse failure is a
property of the untrusted content — the crash class above stays
reserved for actual crashes, and no terminal report promises a retry
the terminal replay path cannot deliver. The only
infrastructure-RETRY class is the PARENT's storage download
(``download_to_file``, see the validation service), plus launch-level
``OSError`` from spawning the child itself (fork failure, missing
python), which propagates.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from os import PathLike
from pathlib import Path

from app.modules.submissions.enums import FileType
from app.modules.submissions.validators.common import (
    ValidationCode,
    ValidationReport,
    ValidationReportBuilder,
    report_from_json,
)

__all__ = [
    "SANDBOX_PARSER_VERSION",
    "SandboxLimits",
    "SandboxedValidation",
    "ValidatorSandbox",
    "WORKER_ENTRY_MODULE",
]

#: The entry module a sandboxed run executes (runnable via
#: ``python -m``); imports ONLY the validator stack.
WORKER_ENTRY_MODULE = "app.modules.submissions.validators.worker_entry"

#: parser_version recorded in synthetic timeout/crash reports — the
#: run never reached a format parser, so the sandbox itself is the
#: reporting component.
SANDBOX_PARSER_VERSION = "sandbox-1"

#: How much of the child's stderr is kept for server-side logs.
_STDERR_TAIL_CHARS = 2000

#: The backend directory (parent of the ``app`` package): the child's
#: working directory, so ``-m app.modules...`` resolves in a repo
#: checkout without relying on an installed package.
_BACKEND_DIR = Path(__file__).resolve().parents[3]

Source = str | PathLike[str]


@dataclass(frozen=True, slots=True)
class SandboxLimits:
    """The hard bounds of one sandboxed validator run.

    Deployment defaults are Settings-configurable
    (``validation_wall_timeout_seconds`` 120 / ``validation_memory_limit_mb``
    1024 / ``validation_cpu_seconds`` 60 — sized so legitimate work
    under the 200 MB upload cap and the validators' own preflight
    bounds always fits, while the parked ~5x total-cap amplification
    hits the memory wall first).
    """

    wall_timeout_seconds: float
    memory_limit_bytes: int
    cpu_seconds: int

    def __post_init__(self) -> None:
        if self.wall_timeout_seconds <= 0:
            raise ValueError("wall_timeout_seconds 必须是正数")
        if self.memory_limit_bytes <= 0:
            raise ValueError("memory_limit_bytes 必须是正数")
        if self.cpu_seconds <= 0:
            raise ValueError("cpu_seconds 必须是正整数")


@dataclass(frozen=True, slots=True)
class SandboxedValidation:
    """What one sandboxed run answered.

    ``report`` is the child's §12.4 report on the success path, the
    synthetic stable-code report on timeout/crash, and ``None`` when
    detection answered "none of the three types" (``detected_type``
    null) — no parser ran, and the FILE_TYPE_NOT_ALLOWED verdict is
    the service's to assemble. ``failure_code`` separates the success
    path (None) from the two infrastructure failure classes;
    ``stderr_tail`` is for logs only and never reaches the persisted
    report.
    """

    detected_type: FileType | None
    report: ValidationReport | None
    failure_code: ValidationCode | None
    stderr_tail: str = ""


def _apply_limits(memory_limit_bytes: int, cpu_seconds: int) -> None:
    """Child-side preexec hook: cap address space and CPU time.

    Runs between fork and exec (single-threaded worker context —
    ``preexec_fn`` is unsafe only with threads). Soft and hard limits
    are set equal so the soft-limit signal and the hard-limit kill
    cannot be separated by the child.
    """
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (memory_limit_bytes, memory_limit_bytes))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))


def _failure_report(
    code: ValidationCode, message: str, declared: FileType, duration_ms: float
) -> ValidationReport:
    builder = ValidationReportBuilder(
        parser_version=SANDBOX_PARSER_VERSION, file_type=declared
    )
    builder.add_error(code, message)
    return builder.build(
        row_count=0,
        detected_columns=[],
        missing_required_columns=[],
        extra_columns=[],
        type_error_counts={},
        null_ratios={},
        duplicate_counts={},
        duration_ms=duration_ms,
    )


def _tail(text: str) -> str:
    return text[-_STDERR_TAIL_CHARS:]


class ValidatorSandbox:
    """Runs one validator execution inside a bounded subprocess.

    ``entrypoint`` defaults to the real worker entry
    (``[sys.executable, "-m", WORKER_ENTRY_MODULE]``); tests may point
    it at fixture scripts that speak the same protocol — the sandbox's
    contract is the protocol and the bounds, not any specific child.
    """

    def __init__(
        self,
        *,
        limits: SandboxLimits,
        entrypoint: Sequence[str] | None = None,
    ) -> None:
        self._limits = limits
        self._entrypoint: list[str] = (
            list(entrypoint)
            if entrypoint is not None
            else [sys.executable, "-m", WORKER_ENTRY_MODULE]
        )

    def run(
        self, *, path: Source, request: Mapping[str, object], declared_type: FileType
    ) -> SandboxedValidation:
        """Execute one sandboxed validator run over ``path``.

        ``declared_type`` labels the synthetic failure reports only
        (the declared type is all that is known when the child dies
        before answering). Never raises for timeout/crash/OOM — those
        are outcomes; only launch-level ``OSError`` propagates.
        """
        argv = [*self._entrypoint, str(path)]
        limits = self._limits
        try:
            completed = subprocess.run(
                argv,
                input=json.dumps(request),
                capture_output=True,
                text=True,
                timeout=limits.wall_timeout_seconds,
                cwd=_BACKEND_DIR,
                check=False,
                preexec_fn=lambda: _apply_limits(
                    limits.memory_limit_bytes, limits.cpu_seconds
                ),
            )
        except subprocess.TimeoutExpired as expired:
            # subprocess.run already killed and reaped the child.
            raw_stderr = expired.stderr
            stderr = (
                raw_stderr.decode("utf-8", "replace")
                if isinstance(raw_stderr, bytes)
                else (raw_stderr or "")
            )
            return SandboxedValidation(
                detected_type=None,
                report=_failure_report(
                    ValidationCode.VALIDATION_TIMED_OUT,
                    f"解析超过硬性时间上限 {limits.wall_timeout_seconds} 秒，"
                    "已终止本次校验",
                    declared_type,
                    duration_ms=limits.wall_timeout_seconds * 1000.0,
                ),
                failure_code=ValidationCode.VALIDATION_TIMED_OUT,
                stderr_tail=_tail(stderr),
            )
        if completed.returncode != 0:
            return self._crashed(declared_type, completed.stderr)
        try:
            answer = json.loads(completed.stdout)
            detected_raw = answer["detected_type"]
            detected = FileType(detected_raw) if detected_raw is not None else None
            report = (
                report_from_json(answer["report"])
                if answer["report"] is not None
                else None
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return self._crashed(declared_type, completed.stderr)
        if report is None and detected is not None:
            # A type was detected but no report came back: protocol
            # violation, classified as a crash rather than trusted.
            return self._crashed(declared_type, completed.stderr)
        return SandboxedValidation(
            detected_type=detected,
            report=report,
            failure_code=None,
        )

    def _crashed(self, declared: FileType, stderr: str) -> SandboxedValidation:
        return SandboxedValidation(
            detected_type=None,
            report=_failure_report(
                ValidationCode.VALIDATION_WORKER_CRASHED,
                "校验进程异常终止（内存超限或崩溃），请稍后重试或联系支持",
                declared,
                duration_ms=0.0,
            ),
            failure_code=ValidationCode.VALIDATION_WORKER_CRASHED,
            stderr_tail=_tail(stderr or ""),
        )
