# backend/app/integrations/zip_safety.py
"""Shared ZIP-archive safety preflight (security round F3).

The declared-size and layout checks that must run BEFORE any archive
member is decompressed. Both ZIP consumers in the product share this
core: the submission XLSX validator (which additionally caps
sharedStrings and scans for renamed role parts) and the assignment
import parser (default-deny: no streaming exemptions). The checks are
pure functions over ``zipfile.ZipInfo`` — no I/O, no error-builder
dependency — so each caller maps a violation into its own error
vocabulary (ValidationCode on the submission side, ImportErrorCode on
the import side).

The preflight trusts only the central directory's DECLARED sizes: a
lying directory is rejected without ever inflating a byte, and an
honest bomb is rejected by the ratio/total caps before decompression.
``OSError``/``BadZipFile`` from reading the directory are the CALLER's
concern (the import parser treats them as MALFORMED_XLSX; the
submission validator as its own not-an-xlsx verdict).
"""

from __future__ import annotations

import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "MAX_ARCHIVE_ENTRIES",
    "MAX_COMPRESSION_RATIO",
    "MAX_TOTAL_UNCOMPRESSED",
    "MAX_WHOLE_READ_PART",
    "ZipSafetyKind",
    "ZipSafetyViolation",
    "is_suspicious_name",
    "preflight",
]

#: Archive shape caps, shared by both consumers (values moved from the
#: submission validator; they are product-wide ZIP policy, not
#: submission-specific).
MAX_ARCHIVE_ENTRIES = 4096
MAX_TOTAL_UNCOMPRESSED = 512 * 1024 * 1024
MAX_WHOLE_READ_PART = 16 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100.0


class ZipSafetyKind(StrEnum):
    """Which shared rule fired (callers map this to their vocabulary)."""

    TOO_MANY_ENTRIES = "too_many_entries"
    SUSPICIOUS_NAME = "suspicious_name"
    PART_TOO_LARGE = "part_too_large"
    COMPRESSION_RATIO = "compression_ratio"
    TOTAL_TOO_LARGE = "total_too_large"
    TOTAL_RATIO = "total_ratio"


@dataclass(frozen=True, slots=True)
class ZipSafetyViolation:
    """The first fatal violation, with the numbers for the message."""

    kind: ZipSafetyKind
    name: str = ""  # offending entry, where one exists
    file_size: int = 0  # declared uncompressed size of that entry
    compress_size: int = 0  # declared compressed size of that entry
    entries: int = 0  # entry count, for the archive-level kinds
    total_uncompressed: int = 0  # declared total, for the total kinds


def is_suspicious_name(name: str) -> bool:
    """Extraction-hazard entry names, checked before anything opens."""
    if not name or name.startswith(("/", "\\")):
        return True
    if len(name) > 1 and name[1] == ":":  # Windows drive letter
        return True
    return ".." in name.replace("\\", "/").split("/")


def preflight(
    infos: Sequence[zipfile.ZipInfo],
    *,
    exempt_prefixes: Sequence[str] = (),
    max_entries: int = MAX_ARCHIVE_ENTRIES,
    max_total_uncompressed: int = MAX_TOTAL_UNCOMPRESSED,
    max_whole_read_part: int = MAX_WHOLE_READ_PART,
    max_compression_ratio: float = MAX_COMPRESSION_RATIO,
) -> ZipSafetyViolation | None:
    """First fatal violation in deterministic order: entry count, then
    per-entry (suspicious name, part cap, ratio), then totals (size,
    aggregate ratio). ``None`` = the archive passes.

    ``exempt_prefixes`` names the streaming families exempt from the
    per-part cap (worksheets iterate row-by-row under their own row
    cap; media/drawings are never parsed). A ``.rels`` part is NEVER
    exempt — relationship parts are read whole wherever they sit, so a
    renamed giant ``.rels`` stays capped (default-deny).
    """
    if len(infos) > max_entries:
        return ZipSafetyViolation(ZipSafetyKind.TOO_MANY_ENTRIES, entries=len(infos))

    total_uncompressed = 0
    total_compressed = 0
    for info in infos:
        name = info.filename
        if is_suspicious_name(name):
            return ZipSafetyViolation(ZipSafetyKind.SUSPICIOUS_NAME, name=name)
        capped = name.endswith(".rels") or not name.startswith(tuple(exempt_prefixes))
        if capped and info.file_size > max_whole_read_part:
            return ZipSafetyViolation(
                ZipSafetyKind.PART_TOO_LARGE,
                name=name,
                file_size=info.file_size,
            )
        if info.file_size and (
            info.compress_size == 0
            or info.file_size / info.compress_size > max_compression_ratio
        ):
            return ZipSafetyViolation(
                ZipSafetyKind.COMPRESSION_RATIO,
                name=name,
                file_size=info.file_size,
                compress_size=info.compress_size,
            )
        total_uncompressed += info.file_size
        total_compressed += info.compress_size
    if total_uncompressed > max_total_uncompressed:
        return ZipSafetyViolation(
            ZipSafetyKind.TOTAL_TOO_LARGE,
            total_uncompressed=total_uncompressed,
        )
    if total_compressed and (
        total_uncompressed / total_compressed > max_compression_ratio
    ):
        return ZipSafetyViolation(
            ZipSafetyKind.TOTAL_RATIO,
            total_uncompressed=total_uncompressed,
            compress_size=total_compressed,
        )
    return None
