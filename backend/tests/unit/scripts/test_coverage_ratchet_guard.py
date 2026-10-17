# backend/tests/unit/scripts/test_coverage_ratchet_guard.py
"""The out-of-range arc guard (B-item follow-up, 2026-10-17).

SQLAlchemy's AsyncSession bridges to its sync core over greenlet. With
the concurrency library undeclared, the tracer's arc bookkeeping crosses
frames at every greenlet switch: arcs whose endpoints are line numbers
of a DIFFERENT file land in this file's data — endpoints beyond this
file's line count. That fingerprint is deterministic in the raw data
even though the report layer silently drops the out-of-range arcs (and
with them the lines they should have credited). The ratchet gate must
refuse to measure such a run instead of reporting phantom misses.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from coverage.sqldata import CoverageData

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "coverage_ratchet.py"


def _load_ratchet_module():
    spec = importlib.util.spec_from_file_location(
        "coverage_ratchet_under_test", _SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write_data(path: Path, file: Path, arcs: list[tuple[int, int]]) -> None:
    data = CoverageData(str(path))
    data.erase()
    data.add_arcs({str(file): arcs})
    data.write()
    data.close()


@pytest.fixture
def small_source(tmp_path: Path) -> Path:
    """A 12-line module — anything past line 12 is beyond EOF."""
    source = tmp_path / "small_module.py"
    source.write_text(
        "def f():\n"  # 1
        "    a = 1\n"  # 2
        "    b = 2\n"  # 3
        "    c = 3\n"  # 4
        "    d = 4\n"  # 5
        "    return a\n"  # 6
        "\n"  # 7
        "\n"  # 8
        "def g():\n"  # 9
        "    return 2\n"  # 10
        "\n"  # 11
        "\n",  # 12
        encoding="utf-8",
    )
    return source


def test_clean_data_has_no_signatures(tmp_path: Path, small_source: Path) -> None:
    ratchet = _load_ratchet_module()
    data_file = tmp_path / ".coverage"
    _write_data(data_file, small_source, [(1, 2), (2, 3), (3, 6), (6, -1)])

    hits = ratchet._undeclared_concurrency_signatures(str(data_file))

    assert hits == []


def test_out_of_range_arc_endpoint_is_flagged(
    tmp_path: Path, small_source: Path
) -> None:
    """The greenlet cross-talk shape: an await-line arc landing on a
    line number of a different (much longer) file — here 1973 against a
    12-line module."""
    ratchet = _load_ratchet_module()
    data_file = tmp_path / ".coverage"
    _write_data(data_file, small_source, [(1, 2), (2, 1973), (1973, 1985)])

    hits = ratchet._undeclared_concurrency_signatures(str(data_file))

    assert len(hits) == 1
    assert "small_module.py" in hits[0]
    assert "1973" in hits[0]


def test_negative_return_arcs_within_file_are_not_flagged(
    tmp_path: Path, small_source: Path
) -> None:
    """Coverage encodes call-returns as negative target lines; -6 means
    'returned to line 6' and must not be mistaken for cross-talk."""
    ratchet = _load_ratchet_module()
    data_file = tmp_path / ".coverage"
    _write_data(data_file, small_source, [(2, 9), (9, 10), (10, -2)])

    hits = ratchet._undeclared_concurrency_signatures(str(data_file))

    assert hits == []


def test_missing_source_file_is_skipped(tmp_path: Path) -> None:
    """A measured file that no longer exists contributes no signature —
    the guard reports, it never crashes the gate."""
    ratchet = _load_ratchet_module()
    data_file = tmp_path / ".coverage"
    gone = tmp_path / "gone.py"
    gone.write_text("x = 1\n", encoding="utf-8")
    _write_data(data_file, gone, [(1, 2), (2, 5000)])
    gone.unlink()

    hits = ratchet._undeclared_concurrency_signatures(str(data_file))

    assert hits == []


@pytest.mark.parametrize("waiver_index", [0, 1])
def test_known_residual_fingerprint_is_waived(
    tmp_path: Path, waiver_index: int
) -> None:
    """The one researched residual (B item 2026-10-17): coverage's
    greenlet patch still leaks a three-arc signature in full-suite
    combination runs, even with concurrency declared. Cross-talk is
    inherently misfiled: the SAME fingerprint set landed in
    upload_service.py locally and validation_service.py on CI (#101) —
    the waiver matches the arc fingerprint on ANY host file, and any
    drift (a new arc, a different line) stays a violation."""
    ratchet = _load_ratchet_module()
    arcs = sorted(int(n) for n in ratchet._CROSSTALK_WAIVERS[waiver_index]["beyond"])  # type: ignore[index]
    chain = [(1, 2), (2, arcs[0])] + [
        (a, b) for a, b in zip(arcs, arcs[1:], strict=False)
    ]
    chain.append((arcs[-1], -(arcs[0])))
    source = tmp_path / "any_host_file.py"
    source.write_text("x = 1\n" * 12, encoding="utf-8")
    data_file = tmp_path / ".coverage"
    _write_data(data_file, source, chain)

    hits = ratchet._undeclared_concurrency_signatures(str(data_file))

    assert hits == []


def test_waiver_requires_exact_fingerprint_match(tmp_path: Path) -> None:
    """One extra arc beyond the waived set must still be flagged — the
    waiver is a registry of known residuals, not a file-level (let
    alone global) pass."""
    ratchet = _load_ratchet_module()
    source = tmp_path / "any_host_file.py"
    source.write_text("x = 1\n" * 12, encoding="utf-8")
    data_file = tmp_path / ".coverage"
    _write_data(
        data_file,
        source,
        [(1, 2), (2, 3), (3, 2999)],  # 2999 is NOT in the waived set
    )

    hits = ratchet._undeclared_concurrency_signatures(str(data_file))

    assert len(hits) == 1
