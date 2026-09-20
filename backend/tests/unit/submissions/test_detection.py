# backend/tests/unit/submissions/test_detection.py
"""Unit tests for content-based submission type detection (spec §10/§12:
fake .csv/.sqlite must fail by DETECTION, not by extension or declaration;
plan 04 task 7 step 2).

``detect_file_type`` sniffs the FIRST bytes and the archive manifest —
never the filename — and answers one of the three uploadable types or
``None`` for "content unrecognized". Everything here is pure unit work
over temp files: no database, no services.

Detection matrix (spec §38.4 rows this gates):

- a real CSV (plain and BOM'd) -> CSV;
- a real XLSX (ZIP magic + ``[Content_Types].xml`` member) -> XLSX;
- a real SQLite 3 file (header magic) -> SQLITE;
- a random ZIP that is not an OOXML package -> None (binary, not CSV);
- binary garbage declared .csv (ELF header) -> None;
- text with a NUL byte -> None (binary content, not plausible text);
- non-UTF-8 bytes -> None (the V1 encoding policy rejects them);
- an empty file -> CSV-plausible (the validator then reports
  EMPTY_FILE — detection passes structure, the parser judges content);
- XLSX bytes declared SQLITE -> XLSX (the truth, whatever was declared).
"""

from __future__ import annotations

import sqlite3
import zipfile
from pathlib import Path

from openpyxl import Workbook

from app.modules.submissions.enums import FileType
from app.modules.submissions.validators.detect import detect_file_type


def _write(tmp_path: Path, data: bytes, name: str = "upload.bin") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _sqlite_file(tmp_path: Path) -> Path:
    path = tmp_path / "upload.sqlite"
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE t (url TEXT)")
        connection.execute("INSERT INTO t VALUES ('https://example.com')")
        connection.commit()
    finally:
        connection.close()
    return path


def _xlsx_file(tmp_path: Path) -> Path:
    path = tmp_path / "upload.xlsx"
    workbook = Workbook()
    workbook.active.append(("url", "title"))  # type: ignore[union-attr]
    workbook.save(path)
    return path


def test_real_csv_text_is_detected(tmp_path: Path) -> None:
    path = _write(tmp_path, b"url,title\nhttps://a.com,t\n")
    assert detect_file_type(path) is FileType.CSV


def test_utf8_bom_csv_is_detected(tmp_path: Path) -> None:
    path = _write(tmp_path, "﻿url,title\n".encode("utf-8-sig"))
    assert detect_file_type(path) is FileType.CSV


def test_multibyte_character_split_across_sample_boundary_still_csv(
    tmp_path: Path,
) -> None:
    # A CJK header whose last character straddles the sniff boundary must
    # not be misjudged as invalid UTF-8 (the incremental decoder buffers
    # split characters, exactly like the CSV validator's own sniff).
    header = "链接" * 6000  # 18 KB of multibyte text
    path = _write(tmp_path, (header + ",标题\n").encode("utf-8"))
    assert detect_file_type(path) is FileType.CSV


def test_real_xlsx_is_detected(tmp_path: Path) -> None:
    assert detect_file_type(_xlsx_file(tmp_path)) is FileType.XLSX


def test_real_sqlite_is_detected(tmp_path: Path) -> None:
    assert detect_file_type(_sqlite_file(tmp_path)) is FileType.SQLITE


def test_zip_without_content_types_manifest_is_not_any_type(
    tmp_path: Path,
) -> None:
    # ZIP magic but no [Content_Types].xml member: not an XLSX, and being
    # binary it is not plausible CSV either -> unrecognized.
    path = tmp_path / "plain.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("readme.txt", "just a zip")
    assert detect_file_type(path) is None


def test_binary_garbage_declared_csv_is_unrecognized(tmp_path: Path) -> None:
    # Spec §38.4 假 .csv 实际 binary: an ELF-style header decodes as text
    # only in the loosest sense — the NUL bytes rule text out.
    path = _write(tmp_path, b"\x7fELF\x02\x01\x01\x00" + bytes(range(256)))
    assert detect_file_type(path) is None


def test_nul_byte_text_is_unrecognized(tmp_path: Path) -> None:
    path = _write(tmp_path, b"url,title\nhttps://a.com\x00tail,t\n")
    assert detect_file_type(path) is None


def test_non_utf8_bytes_are_unrecognized(tmp_path: Path) -> None:
    # GB18030-encoded text: decodable, but not as UTF-8 — the V1 encoding
    # policy rejects it, so it is not plausible CSV content.
    path = _write(tmp_path, "链接,标题\n".encode("gb18030"))
    assert detect_file_type(path) is None


def test_empty_file_is_csv_plausible(tmp_path: Path) -> None:
    # Detection passes structure; the validator reports EMPTY_FILE.
    path = _write(tmp_path, b"")
    assert detect_file_type(path) is FileType.CSV


def test_xlsx_bytes_are_detected_as_xlsx_whatever_was_declared(
    tmp_path: Path,
) -> None:
    # Spec §38.4 假 .sqlite (and the mirror case): detection answers the
    # CONTENT truth; the declared/allowed comparison is the service's
    # gate, not detection's.
    xlsx = _xlsx_file(tmp_path)
    renamed = tmp_path / "upload.sqlite"
    renamed.write_bytes(xlsx.read_bytes())
    assert detect_file_type(renamed) is FileType.XLSX


def test_sqlite_magic_prefix_of_otherwise_text_is_sqlite(tmp_path: Path) -> None:
    # The 16-byte header wins over any text plausibility.
    path = _write(tmp_path, b"SQLite format 3\x00rest is text")
    assert detect_file_type(path) is FileType.SQLITE


def test_missing_path_is_transient_infrastructure_error(tmp_path: Path) -> None:
    # A vanished file is infrastructure (the worker retries), never a
    # property of the submission: the same policy as the validators.
    missing = tmp_path / "vanished.bin"
    try:
        detect_file_type(missing)
    except OSError:
        pass
    else:
        raise AssertionError("expected OSError for a missing path")


def test_detection_uses_a_bounded_read(tmp_path: Path) -> None:
    # The sniff is one bounded prefix read: a NUL byte placed BEYOND the
    # sample boundary must not flip the verdict — a whole-file sniff
    # would return None here, the bounded one answers CSV.
    sample = 8192
    data = ("x" * (sample - 1)).encode() + b"\n" + b"\x00" * sample
    path = _write(tmp_path, data)
    assert detect_file_type(path) is FileType.CSV
