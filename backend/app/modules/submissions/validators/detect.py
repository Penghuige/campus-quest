# backend/app/modules/submissions/validators/detect.py
"""Content-based submission type detection (spec §10 类型 sniffing, §12;
plan 04 task 7 step 2).

``detect_file_type(path)`` answers which of the three uploadable types
(CSV / XLSX / SQLITE) a file's CONTENT actually is, or ``None`` when the
content matches none of them. It never reads the filename, the client's
declaration, or any extension: a fake ``.csv`` that is really a binary,
or a fake ``.sqlite`` that is really an XLSX, is answered by content
truth so the validation service can fail it with
``FILE_TYPE_NOT_ALLOWED`` (spec §38.4: 假 .csv 实际 binary / 假 .sqlite).

Detection rules (in order; first match wins):

1. **SQLite** — the first 16 bytes equal the SQLite 3 header magic
   ``"SQLite format 3\\0"`` (the check the SQLite validator re-asserts
   before opening the file).
2. **XLSX** — the file starts with the ZIP local-header magic
   ``PK\\x03\\x04`` AND the archive contains a
   ``[Content_Types].xml`` member (the OOXML package marker). A ZIP
   without the manifest is not an XLSX — and being binary it is not
   plausible CSV either, so it answers ``None``.
3. **CSV** — plausible text: the sniffed prefix decodes as UTF-8
   (optionally BOM'd; an incremental decoder buffers boundary-split
   multibyte characters so a valid file is never falsely rejected) and
   contains no NUL byte. Anything else — non-UTF-8 encodings included,
   matching the V1 encoding policy — answers ``None``.

An empty file is CSV-plausible: detection judges structure, and the
EMPTY_FILE verdict belongs to the format validator that follows.

Resource shape: one bounded prefix read (``_SNIFF_BYTES``) for rules 1
and 3; rule 2 additionally opens the archive through ``zipfile`` to
list member names. THIS MODULE IS BUILT TO RUN INSIDE THE SANDBOXED
VALIDATOR CHILD (spec §33.3 解析器隔离): a hostile archive that turns the
member listing into a memory bomb dies against the child's RLIMIT_AS,
never in the worker process. Do not call it from request handlers.

``OSError`` from reading the file propagates deliberately. In the
production path that means it escapes the CHILD and lands in the
parent's crash class (``VALIDATION_WORKER_CRASHED``): by the time the
child runs, the parent has already downloaded the object to a local
temp file, so a file this code cannot even read is not a retryable
storage condition — the only infrastructure-retry class is the
PARENT's ``download_to_file`` call. (An ``OSError`` during the parse
AFTER detection is different: the child entry converts that into a
structured ``MALFORMED_*`` outcome.)
"""

from __future__ import annotations

import codecs
import zipfile
from os import PathLike
from typing import BinaryIO

from app.modules.submissions.enums import FileType

__all__ = ["detect_file_type"]

#: How much of the file the text/magic sniff reads. Far less than the
#: 200 MB upload cap; large enough that no real header magic or BOM
#: could sit beyond it.
_SNIFF_BYTES = 8192

_SQLITE_MAGIC = b"SQLite format 3\x00"
_ZIP_LOCAL_MAGIC = b"PK\x03\x04"
_OOXML_MANIFEST = "[Content_Types].xml"

Source = str | PathLike[str]


def detect_file_type(path: Source) -> FileType | None:
    """Classify file content into the uploadable universe, or ``None``.

    Raises ``OSError`` for an unreadable/missing path and nothing else
    — every content-level outcome is a return value. In the sandboxed
    child an escaping ``OSError`` here is crash-classified by the
    parent (see the module docstring: the parent's download is the
    only retry path, and it already succeeded).
    """
    with open(path, "rb") as handle:
        head = handle.read(_SNIFF_BYTES)
        if head.startswith(_SQLITE_MAGIC):
            return FileType.SQLITE
        if head.startswith(_ZIP_LOCAL_MAGIC):
            handle.seek(0)
            return _detect_zip(handle)
        return _detect_text(head)


def _detect_zip(handle: BinaryIO) -> FileType | None:
    """ZIP-magic file: XLSX iff the OOXML manifest member is present.

    Runs under the child's resource limits by contract (see module
    docstring); a hostile central directory is the sandbox's problem.
    """
    try:
        with zipfile.ZipFile(handle) as archive:
            if _OOXML_MANIFEST in archive.namelist():
                return FileType.XLSX
    except zipfile.BadZipFile:
        # Truncated/mismatched magic: the content is not a usable
        # archive of any recognized type.
        return None
    return None


def _detect_text(head: bytes) -> FileType | None:
    """Plausible UTF-8 text without NUL bytes -> CSV."""
    if b"\x00" in head:
        return None
    decoder = codecs.getincrementaldecoder("utf-8-sig")()
    try:
        decoder.decode(head)
    except UnicodeDecodeError:
        return None
    return FileType.CSV
