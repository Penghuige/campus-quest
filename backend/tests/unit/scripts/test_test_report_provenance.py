# backend/tests/unit/scripts/test_test_report_provenance.py
"""Artifact provenance for the offline test report.

Every result file the dashboard aggregates can carry a sidecar
``<artifact>.provenance.json`` recording WHERE it came from: the source
commit, whether the workspace was dirty, the producing command, the UTC
timestamp, and the artifact's own content hash at stamp time. The
freshness classification is FAIL-CLOSED:

- source_sha == report HEAD and clean and hash intact -> ``fresh``
- source_sha == report HEAD but the workspace was dirty -> ``dirty``
  (never "commit fresh")
- source_sha != report HEAD -> ``stale``
- missing / corrupt metadata, or the artifact changed after stamping
  (hash mismatch) -> ``unknown`` (never "fresh")

Old artifacts stamped before this scheme (or copied in from elsewhere)
have no sidecar: ``unknown``. The report still generates regardless —
provenance is displayed, never a generation gate.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "test_report.py"

_HEAD = "a" * 40
_OTHER_HEAD = "b" * 40


def _load_report_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "test_report_prov_under_test", _SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def report_module() -> Any:
    return _load_report_module()


def _artifact(
    tmp_path: Path, name: str = "backend-unit.xml", body: str = "<x/>"
) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_sidecar(
    artifact: Path,
    *,
    source_sha: str = _HEAD,
    dirty: bool = False,
    command: str = "pytest",
    artifact_sha256: str | None = None,
    extra: dict[str, Any] | None = None,
) -> Path:
    payload: dict[str, Any] = {
        "schema": 1,
        "source_sha": source_sha,
        "dirty": dirty,
        "artifact_sha256": artifact_sha256 or _sha256_of(artifact),
        "command": command,
        "generated_at_utc": "2026-10-26T00:00:00Z",
    }
    if extra:
        payload.update(extra)
    sidecar = artifact.with_name(artifact.name + ".provenance.json")
    sidecar.write_text(json.dumps(payload), encoding="utf-8")
    return sidecar


# --- stamping --------------------------------------------------------------


def test_stamp_writes_the_sidecar(report_module: Any, tmp_path: Path) -> None:
    """--stamp-artifact records source sha, dirty state, command, UTC
    time, and the artifact's content hash next to the artifact."""
    artifact = _artifact(tmp_path)
    payload = report_module.stamp_artifact(
        artifact,
        source_sha=_HEAD,
        dirty=False,
        command="uv run pytest tests/unit tests/workers",
        generated_at_utc="2026-10-26T12:00:00Z",
    )
    sidecar = artifact.with_name(artifact.name + ".provenance.json")
    assert json.loads(sidecar.read_text(encoding="utf-8")) == {
        "schema": 1,
        "source_sha": _HEAD,
        "dirty": False,
        "artifact_sha256": _sha256_of(artifact),
        "command": "uv run pytest tests/unit tests/workers",
        "generated_at_utc": "2026-10-26T12:00:00Z",
    }
    assert payload["source_sha"] == _HEAD


# --- classification --------------------------------------------------------


def test_matching_sha_clean_and_intact_is_fresh(
    report_module: Any, tmp_path: Path
) -> None:
    artifact = _artifact(tmp_path)
    _write_sidecar(artifact)
    prov = report_module.load_provenance(artifact, head=_HEAD)
    assert prov is not None
    assert prov.state == "fresh"
    assert prov.source_sha == _HEAD
    assert prov.command == "pytest"


def test_same_sha_but_dirty_is_dirty_not_fresh(
    report_module: Any, tmp_path: Path
) -> None:
    artifact = _artifact(tmp_path)
    _write_sidecar(artifact, dirty=True)
    prov = report_module.load_provenance(artifact, head=_HEAD)
    assert prov is not None and prov.state == "dirty"


def test_cross_sha_is_stale(report_module: Any, tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)
    _write_sidecar(artifact, source_sha=_OTHER_HEAD)
    prov = report_module.load_provenance(artifact, head=_HEAD)
    assert prov is not None and prov.state == "stale"


def test_missing_sidecar_is_unknown(report_module: Any, tmp_path: Path) -> None:
    """Old-format compatibility: an artifact stamped before this scheme
    (or copied in) classifies unknown — never fresh."""
    artifact = _artifact(tmp_path)
    prov = report_module.load_provenance(artifact, head=_HEAD)
    assert prov is not None and prov.state == "unknown"


def test_corrupt_sidecar_is_unknown(report_module: Any, tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)
    sidecar = _write_sidecar(artifact)
    sidecar.write_text("{not json", encoding="utf-8")
    prov = report_module.load_provenance(artifact, head=_HEAD)
    assert prov is not None and prov.state == "unknown"


def test_edited_after_stamping_is_unknown(report_module: Any, tmp_path: Path) -> None:
    """The stamped content hash no longer matches the file on disk —
    the artifact was modified after stamping; unknown, never fresh."""
    artifact = _artifact(tmp_path)
    _write_sidecar(artifact)
    artifact.write_text("<changed/>", encoding="utf-8")
    prov = report_module.load_provenance(artifact, head=_HEAD)
    assert prov is not None and prov.state == "unknown"


def test_unknown_head_classifies_unknown(report_module: Any, tmp_path: Path) -> None:
    """Fail-closed: with no HEAD to compare against (git unavailable),
    nothing is called fresh."""
    artifact = _artifact(tmp_path)
    _write_sidecar(artifact)
    prov = report_module.load_provenance(artifact, head="")
    assert prov is not None and prov.state == "unknown"


# --- metadata validity: every required field, or unknown ---------------


def test_missing_schema_is_unknown(report_module: Any, tmp_path: Path) -> None:
    """A sidecar without the schema marker is a shape this script never
    wrote — unknown, never fresh."""
    artifact = _artifact(tmp_path)
    _write_sidecar(artifact, extra={"schema": None})
    sidecar = artifact.with_name(artifact.name + ".provenance.json")
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    del payload["schema"]
    sidecar.write_text(json.dumps(payload), encoding="utf-8")
    prov = report_module.load_provenance(artifact, head=_HEAD)
    assert prov is not None and prov.state == "unknown"


def test_unknown_schema_version_is_unknown(report_module: Any, tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)
    _write_sidecar(artifact, extra={"schema": 2})
    prov = report_module.load_provenance(artifact, head=_HEAD)
    assert prov is not None and prov.state == "unknown"


def test_missing_generated_at_is_unknown(report_module: Any, tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)
    _write_sidecar(artifact)
    sidecar = artifact.with_name(artifact.name + ".provenance.json")
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    del payload["generated_at_utc"]
    sidecar.write_text(json.dumps(payload), encoding="utf-8")
    prov = report_module.load_provenance(artifact, head=_HEAD)
    assert prov is not None and prov.state == "unknown"


def test_non_string_generated_at_is_unknown(report_module: Any, tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)
    _write_sidecar(artifact, extra={"generated_at_utc": 12345})
    prov = report_module.load_provenance(artifact, head=_HEAD)
    assert prov is not None and prov.state == "unknown"


@pytest.mark.parametrize(
    "bad_time",
    ["not-a-time", "2026-10-26 12:00:00", "2026-10-26T12:00:00+08:00", ""],
)
def test_non_utc_generated_at_is_unknown(
    report_module: Any, tmp_path: Path, bad_time: str
) -> None:
    """The timestamp must be the UTC form the stamper writes
    (…Z); a local-time or offset form is invalid metadata."""
    artifact = _artifact(tmp_path)
    _write_sidecar(artifact, extra={"generated_at_utc": bad_time})
    prov = report_module.load_provenance(artifact, head=_HEAD)
    assert prov is not None and prov.state == "unknown"


def test_empty_source_sha_is_unknown(report_module: Any, tmp_path: Path) -> None:
    """An empty source sha is invalid metadata (no commit of record),
    not 'a different commit' — unknown, never stale."""
    artifact = _artifact(tmp_path)
    _write_sidecar(artifact, source_sha="")
    prov = report_module.load_provenance(artifact, head=_HEAD)
    assert prov is not None and prov.state == "unknown"


def test_valid_schema_and_utc_still_classify_by_content(
    report_module: Any, tmp_path: Path
) -> None:
    """Strictness is not a fresh-loosening trick: a fully valid sidecar
    keeps classifying by the sha/dirty/hash rules (stale here)."""
    artifact = _artifact(tmp_path)
    _write_sidecar(artifact, source_sha=_OTHER_HEAD)
    prov = report_module.load_provenance(artifact, head=_HEAD)
    assert prov is not None and prov.state == "stale"


# --- input validity: one closure, every malformed field ---------------


@pytest.mark.parametrize(
    "bad_schema",
    [True, 1.0, "1", 2, None],
)
def test_non_integer_schema_is_unknown(
    report_module: Any, tmp_path: Path, bad_schema: Any
) -> None:
    """Only the INTEGER 1 is the known schema. Python's == would accept
    True and 1.0; JSON booleans and floats are not shapes the stamper
    writes — unknown, never fresh."""
    artifact = _artifact(tmp_path)
    _write_sidecar(artifact, extra={"schema": bad_schema})
    prov = report_module.load_provenance(artifact, head=_HEAD)
    assert prov is not None and prov.state == "unknown"


@pytest.mark.parametrize("bad_command", ["", "   ", "\t\n"])
def test_blank_command_is_unknown(
    report_module: Any, tmp_path: Path, bad_command: str
) -> None:
    """An unidentifiable producing command is invalid provenance — the
    sidecar must name the command that made the artifact."""
    artifact = _artifact(tmp_path)
    _write_sidecar(artifact, extra={"command": bad_command})
    prov = report_module.load_provenance(artifact, head=_HEAD)
    assert prov is not None and prov.state == "unknown"


@pytest.mark.parametrize(
    "bad_sha",
    ["a" * 39, "a" * 41, "z" * 40, "A" * 40, "abc", ""],
)
def test_malformed_source_sha_is_unknown(
    report_module: Any, tmp_path: Path, bad_sha: str
) -> None:
    """The source must be a full 40-hex LOWERCASE sha — the exact form
    git rev-parse emits and the stamper records. Anything else
    (truncated, non-hex, uppercased, empty) is invalid metadata:
    unknown, never stale (a malformed sha is not 'a different
    commit')."""
    artifact = _artifact(tmp_path)
    _write_sidecar(artifact, source_sha=bad_sha)
    prov = report_module.load_provenance(artifact, head=_HEAD)
    assert prov is not None and prov.state == "unknown"


def test_lowercase_40_hex_sha_comparisons_classify_normally(
    report_module: Any, tmp_path: Path
) -> None:
    """The case policy's control: a proper lowercase 40-hex sha keeps
    the content rules — fresh on a match, never affected by the
    malformed-sha closure."""
    artifact = _artifact(tmp_path)
    _write_sidecar(artifact, command="uv run pytest tests/unit")
    prov = report_module.load_provenance(artifact, head=_HEAD)
    assert prov is not None and prov.state == "fresh"


# --- report integration ----------------------------------------------------


def test_layers_carry_their_provenance_and_the_summary_names_mixed(
    report_module: Any, tmp_path: Path
) -> None:
    """Each layer's artifact is judged independently; the report's
    provenance summary names the per-state counts (mixed runs are
    visible as such, not collapsed into one verdict)."""
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    fresh = _artifact(artifacts, "backend-unit.xml")
    _write_sidecar(fresh)
    stale = _artifact(artifacts, "backend-integration.xml")
    _write_sidecar(stale, source_sha=_OTHER_HEAD)
    _artifact(artifacts, "backend-e2e.xml")  # no sidecar -> unknown

    report = report_module.build_report(
        artifacts,
        branch="test",
        command="pytest",
        generated_at="2026-10-26 00:00:00 UTC",
        commit=_HEAD,
    )
    by_key = {layer.key: layer for layer in report.layers}
    assert by_key["backend-unit"].provenance is not None
    assert by_key["backend-unit"].provenance.state == "fresh"
    assert by_key["backend-integration"].provenance.state == "stale"
    assert by_key["backend-e2e"].provenance.state == "unknown"

    summary = report_module.provenance_summary(report)
    assert summary["fresh"] == 1
    assert summary["stale"] == 1
    assert summary["unknown"] == 1


def test_missing_layers_have_no_provenance_not_unknown(
    report_module: Any, tmp_path: Path
) -> None:
    """A layer with no result file is 未运行 — there is nothing to
    attribute, so it contributes no provenance row at all."""
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    report = report_module.build_report(
        artifacts,
        branch="test",
        command="pytest",
        generated_at="2026-10-26 00:00:00 UTC",
        commit=_HEAD,
    )
    assert all(layer.provenance is None for layer in report.layers)
    assert report_module.provenance_summary(report) == {}


def test_shared_artifact_feeds_both_playwright_layers(
    report_module: Any, tmp_path: Path
) -> None:
    """playwright.json backs two layers (battery + pixel); both cite
    the same artifact's provenance."""
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    shared = artifacts / "playwright.json"
    shared.write_text('{"suites": [], "errors": []}', encoding="utf-8")
    _write_sidecar(shared)
    report = report_module.build_report(
        artifacts,
        branch="test",
        command="pytest",
        generated_at="2026-10-26 00:00:00 UTC",
        commit=_HEAD,
    )
    by_key = {layer.key: layer for layer in report.layers}
    for key in ("playwright", "pixel"):
        assert by_key[key].provenance is not None
        assert by_key[key].provenance.state == "fresh"
