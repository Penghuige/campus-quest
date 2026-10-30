"""Unit tests for the synthetic homework-file generator (plan Task 4).

The declared_type families are the structured three (spec amendment
2026-10-30: one task cannot mix structured and document types, and the
hot task is structured). Files must be really parseable — the async
validation pipeline parses them for real after upload-complete.
"""

from __future__ import annotations

import csv
import io
import random
import sqlite3

import openpyxl

from loadtest.files import pick_profile, synthetic_file

# spec file profile (weights and size bands)
_CSV_WEIGHT, _XLSX_WEIGHT, _SQLITE_WEIGHT = 0.6, 0.3, 0.1


def test_pick_profile_matches_weights_and_bands() -> None:
    rng = random.Random(20261030)
    draws = [pick_profile(rng) for _ in range(500)]
    families = [family for family, _ in draws]

    def share(family: str) -> float:
        return families.count(family) / len(draws)

    assert abs(share("CSV") - _CSV_WEIGHT) <= 0.08
    assert abs(share("XLSX") - _XLSX_WEIGHT) <= 0.08
    assert abs(share("SQLITE") - _SQLITE_WEIGHT) <= 0.08
    assert set(families) <= {"CSV", "XLSX", "SQLITE"}

    for family, target in draws:
        low, high = {
            "CSV": (100_000, 2_000_000),
            "XLSX": (2_000_000, 10_000_000),
            "SQLITE": (10_000_000, 50_000_000),
        }[family]
        assert low <= target <= high, (family, target)


def test_synthetic_csv_parses_with_header() -> None:
    rng = random.Random(1)
    payload = synthetic_file(rng, "CSV", 150_000)

    assert payload.startswith(b"id,payload")
    rows = list(csv.reader(io.StringIO(payload.decode())))
    assert rows[0] == ["id", "payload"]
    assert len(rows) > 100
    # lands inside the small band (client declares actual size)
    assert 100_000 <= len(payload) <= 400_000


def test_synthetic_xlsx_opens_via_openpyxl() -> None:
    rng = random.Random(2)
    payload = synthetic_file(rng, "XLSX", 250_000)

    workbook = openpyxl.load_workbook(io.BytesIO(payload), read_only=True)
    sheet = workbook.active
    rows = sheet.iter_rows(values_only=True)
    header = next(rows)
    assert header == ("id", "payload")
    second = next(rows)
    assert second[0] == 1 and isinstance(second[1], str)
    assert len(payload) >= 100_000


def test_synthetic_sqlite_is_a_real_database() -> None:
    rng = random.Random(3)
    payload = synthetic_file(rng, "SQLITE", 200_000)

    connection = sqlite3.connect(":memory:")
    connection.deserialize(payload)
    count = connection.execute("SELECT COUNT(*) FROM filler").fetchone()[0]
    assert count > 0
    connection.close()


def test_rejects_unknown_family() -> None:
    import pytest

    rng = random.Random(4)
    with pytest.raises(ValueError, match="unsupported declared_type"):
        synthetic_file(rng, "DOCX", 1000)
