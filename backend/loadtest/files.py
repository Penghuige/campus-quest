"""Synthetic homework files for the structured families (spec §Scenarios S1).

One task cannot mix structured and document types (publish validation),
and the hot task is structured, so the generator builds CSV/XLSX/SQLITE
only. Files are really parseable — the async validation pipeline parses
them after upload-complete. Sizes land inside the profile's band; the
client declares the ACTUAL size to the API (pinned_content_length), so
exact target-matching is deliberately not attempted.
"""

from __future__ import annotations

import io
import random
import sqlite3

# spec file profile: family -> (weight, min_bytes, max_bytes)
PROFILE: dict[str, tuple[float, int, int]] = {
    "CSV": (0.6, 100_000, 2_000_000),
    "XLSX": (0.3, 2_000_000, 10_000_000),
    "SQLITE": (0.1, 10_000_000, 50_000_000),
}


def pick_profile(rng: random.Random) -> tuple[str, int]:
    """Draw one (declared_type, target_bytes) per the spec mix."""

    families = list(PROFILE)
    weights = [PROFILE[family][0] for family in families]
    family = rng.choices(families, weights=weights, k=1)[0]
    low, high = PROFILE[family][1], PROFILE[family][2]
    return family, rng.randint(low, high)


def synthetic_file(rng: random.Random, declared_type: str, target_bytes: int) -> bytes:
    """Build one parseable file of roughly ``target_bytes``."""

    if declared_type == "CSV":
        return _csv(rng, target_bytes)
    if declared_type == "XLSX":
        return _xlsx(rng, target_bytes)
    if declared_type == "SQLITE":
        return _sqlite(rng, target_bytes)
    raise ValueError(f"unsupported declared_type: {declared_type!r}")


def _payload(rng: random.Random) -> str:
    return rng.randbytes(45).hex()


def _csv(rng: random.Random, target_bytes: int) -> bytes:
    buffer = io.StringIO()
    buffer.write("id,payload\n")
    index = 1
    while buffer.tell() < target_bytes:
        buffer.write(f"{index},{_payload(rng)}\n")
        index += 1
    return buffer.getvalue().encode()


def _xlsx(rng: random.Random, target_bytes: int) -> bytes:
    from openpyxl import Workbook

    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet("data")
    sheet.append(("id", "payload"))
    # ~90 incompressible bytes per row (hex payload): rows ≈ target/90
    row_count = max(2, target_bytes // 90)
    for index in range(1, row_count + 1):
        sheet.append((index, _payload(rng)))
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _sqlite(rng: random.Random, target_bytes: int) -> bytes:
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE filler (id INTEGER PRIMARY KEY, data BLOB)")
    index = 1
    while True:
        blob = rng.randbytes(8192)
        connection.execute("INSERT INTO filler (id, data) VALUES (?, ?)", (index, blob))
        index += 1
        if index % 8 == 0:  # size checks are not free; check every 64 KiB
            payload = connection.serialize()
            if len(payload) >= target_bytes:
                connection.close()
                return payload
