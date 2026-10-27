# backend/scripts/test_report.py
"""Generate the offline CampusQuest test dashboard.

The dashboard is a single static HTML file (``reports/test-report.html``)
built from per-suite result files in ``reports/artifacts/``:

* pytest JUnit XML for backend unit+worker, integration, and e2e;
* Node ``node:test`` JUnit XML for the frontend unit suite;
* the Playwright JSON reporter for the browser battery (spec files whose
  names contain ``visual`` are routed to the pixel-regression layer);
* an exploration manifest written by the exploration agents.

The fixed seven layers and the four block states are a product contract
from the owner-approved design: green = passed, red = failed, grey =
legitimately skipped, dashed = not run because no result file exists.

Usage:
    uv run python scripts/test_report.py \
        --artifacts-dir ../reports/artifacts \
        --output ../reports/test-report.html

Exit code: 0 when the report was written (even if tests are red — the
report exists to show them), 1 when an artifact cannot be parsed or the
output cannot be written.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

Status = Literal["passed", "failed", "skipped", "not_run"]
LocatorStyle = Literal["pytest", "node"]
ProvenanceState = Literal["fresh", "dirty", "stale", "unknown"]

_SIDECAR_SUFFIX = ".provenance.json"

#: The exact source-sha shape the stamper records (and git emits):
#: 40 lowercase hex characters. Malformed or uppercased values are
#: invalid metadata, not "a different commit".
_SHA40_RE = re.compile(r"[0-9a-f]{40}")

PROVENANCE_LABELS: dict[ProvenanceState, str] = {
    "fresh": "fresh",
    "dirty": "dirty",
    "stale": "stale",
    "unknown": "来源未知",
}

_STATUSES: tuple[Status, ...] = ("passed", "failed", "skipped", "not_run")

STATUS_LABELS: dict[Status, str] = {
    "passed": "通过",
    "failed": "失败",
    "skipped": "合法跳过",
    "not_run": "未运行",
}

_LAYER_TITLES = {
    "backend-unit": "后端单测 + worker",
    "backend-integration": "后端集成",
    "backend-e2e": "后端全栈 e2e",
    "frontend-unit": "前端单测",
    "playwright": "Playwright 电池",
    "pixel": "像素回归",
    "exploration": "探索层",
}

_BACKEND_UNIT = "backend-unit.xml"
_BACKEND_INTEGRATION = "backend-integration.xml"
_BACKEND_E2E = "backend-e2e.xml"
_FRONTEND_UNIT = "frontend-unit.xml"
_PLAYWRIGHT = "playwright.json"
_EXPLORATION = "exploration.json"


class ReportError(RuntimeError):
    """An artifact is missing in a way that cannot be rendered honestly."""


@dataclass(frozen=True)
class TestCase:
    """One square in the dashboard."""

    name: str
    file: str
    case: str
    status: Status
    duration_ms: float | None = None
    message: str = ""

    @property
    def locator(self) -> str:
        if self.file and self.case:
            return f"{self.file}::{self.case}"
        if self.file:
            return self.file
        return self.case or self.name


@dataclass
class LayerReport:
    """One fixed layer row."""

    key: str
    title: str
    artifact: str
    cases: list[TestCase] = field(default_factory=list)
    missing: bool = False
    provenance: ArtifactProvenance | None = None


@dataclass(frozen=True)
class CiJobSummary:
    """One GitHub Actions job as the header renders it."""

    name: str
    state: (
        str  # success / failure / cancelled / skipped / timed_out / pending / neutral
    )


@dataclass(frozen=True)
class CiSummary:
    """The commit's CI verdict, loaded from the ci_record JSON."""

    commit: str
    status: str
    conclusion: str | None
    run_url: str | None
    jobs: tuple[CiJobSummary, ...]
    error: str | None = None


@dataclass(frozen=True)
class MatrixRef:
    """One spec-test matrix row that cites a suite location."""

    matrix: str
    row_id: str
    expectation: str


# The citation grammar check_test_matrix.py enforces, reused for linking.
_CITE = re.compile(r"`(tests/[^`:]+|frontend/(?:e2e|src)/[^`:]+)(?:::([^`]+))?`")
_ROW_ID = re.compile(r"^[A-Z]+-?\d+$|^[EG]\d+$")
_RULE_REF = re.compile(r"^§[\d.\s/§，,+（）()·]+$")


# ---------------------------------------------------------------------------
# Artifact provenance — fail-closed freshness
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactProvenance:
    """Where one result file came from, classified against the report's
    HEAD commit.

    ``fresh``    same commit, clean workspace, artifact byte-identical
                 to the stamped hash;
    ``dirty``    same commit but the workspace was dirty at stamp time
                 — never called commit-fresh;
    ``stale``    a different commit produced it;
    ``unknown``  missing/corrupt metadata, an unknown comparison base,
                 or the artifact changed after stamping — never fresh.
    """

    state: ProvenanceState
    source_sha: str
    dirty: bool
    command: str
    generated_at_utc: str


def sidecar_path(artifact: Path) -> Path:
    return artifact.with_name(artifact.name + _SIDECAR_SUFFIX)


def stamp_artifact(
    artifact: Path,
    *,
    source_sha: str,
    dirty: bool,
    command: str,
    generated_at_utc: str,
) -> dict[str, Any]:
    """Write ``<artifact>.provenance.json`` next to the result file."""
    payload: dict[str, Any] = {
        "schema": 1,
        "source_sha": source_sha,
        "dirty": dirty,
        "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "command": command,
        "generated_at_utc": generated_at_utc,
    }
    sidecar_path(artifact).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return payload


def load_provenance(artifact: Path, *, head: str) -> ArtifactProvenance | None:
    """Classify one artifact's provenance against ``head``.

    None means the artifact itself is absent (nothing to attribute).
    An existing artifact always classifies — invalid or incomplete
    metadata degrades to ``unknown``, never to fresh or stale.

    Input validity is closed over EVERY field before classification:
    the schema must be the INTEGER 1 (bool/float/string forms that
    compare equal are not shapes the stamper writes); the source sha
    must be a full 40-hex LOWERCASE commit sha (the exact form git
    rev-parse emits and the stamper records — comparison is
    lowercase-on-lowercase, and an uppercase or malformed sha is
    invalid metadata, not "a different commit"); the command must be
    a non-blank string; the hash/dirty/timestamp rules are unchanged.
    """
    if not artifact.is_file():
        return None
    try:
        payload_any = json.loads(sidecar_path(artifact).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload_any = None
    if not isinstance(payload_any, dict):
        return ArtifactProvenance("unknown", "", False, "", "")
    payload = cast(dict[str, Any], payload_any)
    source_sha = payload.get("source_sha")
    command = payload.get("command")
    recorded_hash = payload.get("artifact_sha256")
    dirty = payload.get("dirty")
    schema = payload.get("schema")
    generated_at = payload.get("generated_at_utc")
    if not (
        type(schema) is int
        and schema == 1
        and isinstance(source_sha, str)
        and _SHA40_RE.fullmatch(source_sha) is not None
        and isinstance(command, str)
        and command.strip() != ""
        and isinstance(recorded_hash, str)
        and isinstance(dirty, bool)
        and isinstance(generated_at, str)
        and _is_utc_stamp(generated_at)
    ):
        # Anything the stamper would not have written is metadata this
        # classification cannot stand on.
        return ArtifactProvenance("unknown", "", False, "", "")
    try:
        intact = recorded_hash == hashlib.sha256(artifact.read_bytes()).hexdigest()
    except OSError:
        intact = False
    state: ProvenanceState
    if not intact:
        state = "unknown"  # edited (or unreadable) after stamping
    elif not head:
        state = "unknown"  # no comparison base — fail closed
    elif source_sha != head:
        state = "stale"
    elif dirty:
        state = "dirty"
    else:
        state = "fresh"
    return ArtifactProvenance(
        state=state,
        source_sha=source_sha,
        dirty=dirty,
        command=command,
        generated_at_utc=generated_at,
    )


def _is_utc_stamp(value: str) -> bool:
    """The stamper writes ``%Y-%m-%dT%H:%M:%SZ``; that exact UTC form
    (and only it) is valid provenance metadata."""
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def parse_matrices(matrix_dir: Path) -> dict[tuple[str, str | None], list[MatrixRef]]:
    """Index every matrix row's citations: (file, case|None) -> refs.

    The generator's suite paths are backend/frontend-root-relative while
    matrix citations may carry a "frontend/" prefix — normalized here.
    A missing directory yields {} (the link layer degrades silently).
    """
    refs: dict[tuple[str, str | None], list[MatrixRef]] = {}
    if not matrix_dir.is_dir():
        return refs
    for path in sorted(matrix_dir.glob("*.md")):
        matrix = path.stem
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not (stripped.startswith("|") and stripped.endswith("|")):
                continue
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if len(cells) < 4 or not _ROW_ID.match(cells[0]):
                continue
            row_id = cells[0]
            # The expectation cell: the longest prose cell that is neither
            # the rule reference, a citation, nor the status column (the
            # 28 matrices vary in column layout; prose wins by length).
            candidates = [
                cell
                for cell in cells[1:-1]
                if cell and not _CITE.search(cell) and not _RULE_REF.match(cell)
            ]
            expectation = max(candidates, key=len) if candidates else cells[1]
            row_text = " ".join(cells)
            for cited, name in _CITE.findall(row_text):
                key_file = cited[9:] if cited.startswith("frontend/") else cited
                refs.setdefault((key_file, name.strip() or None), []).append(
                    MatrixRef(matrix=matrix, row_id=row_id, expectation=expectation)
                )
    return refs


@dataclass
class Report:
    """Everything the renderer needs for one run."""

    branch: str
    command: str
    generated_at: str
    layers: list[LayerReport]
    notes: list[str]
    artifacts_dir: Path
    commit: str = ""
    ci: CiSummary | None = None
    matrix_refs: dict[tuple[str, str | None], list[MatrixRef]] = field(
        default_factory=dict
    )


# ---------------------------------------------------------------------------
# JUnit adapter (pytest and Node's built-in junit reporter)
# ---------------------------------------------------------------------------


def parse_junit(
    path: Path, *, locator_style: LocatorStyle = "pytest"
) -> list[TestCase]:
    """Parse one JUnit XML file into dashboard cases.

    pytest names a testcase ``classname="tests.unit.test_mod"`` +
    ``name="test_x"`` (or ``classname="...TestThing"`` for class-based
    tests); Node's junit reporter emits ``classname="test"`` and no file
    path at all. The locator is therefore best-effort for Node, which the
    operations doc states explicitly.
    """
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise ReportError(f"cannot parse JUnit XML {path}: {exc}") from exc

    cases: list[TestCase] = []
    for element in root.iter("testcase"):
        name = (element.get("name") or "").strip() or "(unnamed test)"
        classname = (element.get("classname") or "").strip()

        failure = element.find("failure")
        error = element.find("error")
        skipped = element.find("skipped")

        if failure is not None or error is not None:
            status: Status = "failed"
            message = _element_message(failure if failure is not None else error)
        elif skipped is not None:
            status = "skipped"
            message = _element_message(skipped)
        else:
            status = "passed"
            message = ""

        file, case = _junit_locator(classname, name, locator_style)
        cases.append(
            TestCase(
                name=name,
                file=file,
                case=case,
                status=status,
                duration_ms=_seconds_to_ms(element.get("time")),
                message=message,
            )
        )
    return cases


def _element_message(element: ET.Element | None) -> str:
    if element is None:
        return ""
    message = (element.get("message") or "").strip()
    text = (element.text or "").strip()
    if message and text:
        return f"{message}\n{text}"
    return message or text


def _seconds_to_ms(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value) * 1000.0
    except ValueError:
        return None


def _junit_locator(classname: str, name: str, style: LocatorStyle) -> tuple[str, str]:
    if style == "node":
        return classname, name
    if not classname:
        return "", name

    parts = classname.split(".")
    # pytest class-based tests append the class to the dotted module
    # path (e.g. ``tests.unit.test_mod.TestThing``); a lowercase last
    # segment is a module.
    if parts[-1][:1].isupper():
        module = ".".join(parts[:-1])
        return f"{module.replace('.', '/')}.py", f"{parts[-1]}::{name}"
    return f"{classname.replace('.', '/')}.py", name


# ---------------------------------------------------------------------------
# Playwright JSON adapter
# ---------------------------------------------------------------------------


def parse_playwright(path: Path) -> list[TestCase]:
    """Parse the Playwright JSON reporter output.

    ``flaky`` is red: a test that needed a retry is an instability
    signal, and the four-state design has no amber state. Test-level
    global errors become one red synthetic square so a broken webServer
    cannot disappear behind an empty suite list.
    """
    try:
        payload_any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportError(f"cannot read Playwright JSON {path}: {exc}") from exc
    if not isinstance(payload_any, dict):
        raise ReportError(f"{path}: expected a Playwright JSON object")
    payload = cast(dict[str, Any], payload_any)

    cases: list[TestCase] = []
    suites = payload.get("suites")
    if suites is not None and not isinstance(suites, list):
        raise ReportError(f"{path}: expected a 'suites' array")
    for suite in suites or []:
        _walk_playwright_suite(suite, (), "", cases, path)

    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        cases.insert(
            0,
            TestCase(
                name="Playwright 全局错误",
                file=str(path),
                case="errors",
                status="failed",
                message=_playwright_errors_text(errors),
            ),
        )
    return cases


def _walk_playwright_suite(
    suite: object,
    parent_titles: tuple[str, ...],
    inherited_file: str,
    cases: list[TestCase],
    source: Path,
) -> None:
    if not isinstance(suite, dict):
        raise ReportError(f"{source}: a Playwright suite is not an object")
    suite_dict = cast(dict[str, Any], suite)

    file_raw = suite_dict.get("file")
    file = file_raw if isinstance(file_raw, str) and file_raw else inherited_file
    title_raw = suite_dict.get("title")
    title = title_raw if isinstance(title_raw, str) else ""
    titles = parent_titles
    if title and title != file and not _looks_like_spec_file(title):
        titles = (*parent_titles, title)

    specs = suite_dict.get("specs")
    if specs is not None and not isinstance(specs, list):
        raise ReportError(f"{source}: a Playwright suite's 'specs' is not an array")
    for spec_raw in specs or []:
        _walk_playwright_spec(spec_raw, titles, file, cases, source)

    children = suite_dict.get("suites")
    if children is not None and not isinstance(children, list):
        raise ReportError(f"{source}: a Playwright suite's 'suites' is not an array")
    for child in children or []:
        _walk_playwright_suite(child, titles, file, cases, source)


def _walk_playwright_spec(
    spec_raw: object,
    titles: tuple[str, ...],
    inherited_file: str,
    cases: list[TestCase],
    source: Path,
) -> None:
    if not isinstance(spec_raw, dict):
        raise ReportError(f"{source}: a Playwright spec is not an object")
    spec = cast(dict[str, Any], spec_raw)

    spec_file_raw = spec.get("file")
    spec_file = (
        spec_file_raw
        if isinstance(spec_file_raw, str) and spec_file_raw
        else inherited_file
    )
    spec_title_raw = spec.get("title")
    spec_title = (
        spec_title_raw
        if isinstance(spec_title_raw, str) and spec_title_raw
        else "(untitled spec)"
    )
    case_name = " › ".join((*titles, spec_title))

    tests = spec.get("tests")
    if tests is not None and not isinstance(tests, list):
        raise ReportError(f"{source}: a Playwright spec's 'tests' is not an array")
    for test_raw in tests or []:
        if not isinstance(test_raw, dict):
            raise ReportError(f"{source}: a Playwright test is not an object")
        test = cast(dict[str, Any], test_raw)
        results = _playwright_results(test)
        project_raw = test.get("projectName")
        project = project_raw if isinstance(project_raw, str) else ""
        status_raw = test.get("status")
        status_text = status_raw if isinstance(status_raw, str) else ""
        status, message = _playwright_status(status_text, results)
        display_name = f"{case_name} [{project}]" if project else case_name
        cases.append(
            TestCase(
                name=display_name,
                file=spec_file,
                case=case_name,
                status=status,
                duration_ms=_playwright_duration(results),
                message=message,
            )
        )


def _playwright_results(test: dict[str, Any]) -> list[dict[str, Any]]:
    results_raw = test.get("results")
    if not isinstance(results_raw, list):
        return []
    return [
        cast(dict[str, Any], item) for item in results_raw if isinstance(item, dict)
    ]


def _playwright_status(
    status: str, results: list[dict[str, Any]]
) -> tuple[Status, str]:
    if status == "expected":
        return "passed", ""
    if status == "unexpected":
        return "failed", _playwright_failure_message(results)
    if status == "flaky":
        detail = _playwright_failure_message(results)
        prefix = "flaky（重试后通过）"
        return "failed", f"{prefix}\n{detail}".strip()
    if status == "skipped":
        return "skipped", ""

    result_statuses = [str(result.get("status") or "") for result in results]
    if any(value in {"failed", "timedOut", "interrupted"} for value in result_statuses):
        return "failed", _playwright_failure_message(results)
    if any(value == "passed" for value in result_statuses):
        return "passed", ""
    if result_statuses and all(value == "skipped" for value in result_statuses):
        return "skipped", ""
    raise ReportError(f"unknown Playwright test status: {status!r}")


def _playwright_duration(results: list[dict[str, Any]]) -> float | None:
    if not results:
        return None
    total = 0.0
    for result in results:
        value = result.get("duration")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total += float(value)
    return total


def _playwright_failure_message(results: list[dict[str, Any]]) -> str:
    for result in reversed(results):
        if result.get("status") not in {"failed", "timedOut", "interrupted"}:
            continue
        errors = result.get("errors")
        if isinstance(errors, list):
            messages = [
                text
                for error in errors
                if isinstance(error, dict)
                for text in [_playwright_error_text(cast(dict[str, Any], error))]
                if text
            ]
            if messages:
                return "\n".join(messages)
        error = result.get("error")
        if isinstance(error, dict):
            text = _playwright_error_text(cast(dict[str, Any], error))
            if text:
                return text
        if isinstance(error, str) and error.strip():
            return error.strip()
    return ""


def _playwright_error_text(error: dict[str, Any]) -> str:
    for key in ("message", "stack"):
        value = error.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _playwright_errors_text(errors: list[Any]) -> str:
    messages = [
        text
        for error in errors
        if isinstance(error, dict)
        for text in [_playwright_error_text(cast(dict[str, Any], error))]
        if text
    ]
    if messages:
        return "\n".join(messages)
    if any(isinstance(error, str) and error.strip() for error in errors):
        return "\n".join(
            error.strip()
            for error in errors
            if isinstance(error, str) and error.strip()
        )
    return "Playwright reported global errors"


def _looks_like_spec_file(title: str) -> bool:
    return title.endswith((".spec.ts", ".spec.js", ".spec.mjs", ".test.ts", ".test.js"))


def is_visual_spec(file: str) -> bool:
    """Pixel-regression specs are the ones whose filename says visual."""
    return "visual" in Path(file).name.lower()


# ---------------------------------------------------------------------------
# Exploration manifest adapter
# ---------------------------------------------------------------------------


def parse_exploration(path: Path) -> list[TestCase]:
    """Parse the exploration layer's JSON manifest (see the ops doc)."""
    try:
        payload_any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportError(f"cannot read exploration manifest {path}: {exc}") from exc
    if not isinstance(payload_any, dict):
        raise ReportError(f"{path}: expected an exploration manifest object")
    payload = cast(dict[str, Any], payload_any)

    probes = payload.get("probes")
    if not isinstance(probes, list):
        raise ReportError(f'{path}: expected a top-level "probes" array')

    cases: list[TestCase] = []
    for index, probe_raw in enumerate(probes, start=1):
        if not isinstance(probe_raw, dict):
            raise ReportError(f"{path}: probe #{index} is not an object")
        probe = cast(dict[str, Any], probe_raw)

        name_raw = probe.get("name")
        if not isinstance(name_raw, str) or not name_raw.strip():
            raise ReportError(f"{path}: probe #{index} has no name")
        name = name_raw.strip()

        status_raw = probe.get("status")
        if status_raw not in _STATUSES:
            raise ReportError(
                f"{path}: probe {name!r} has invalid status {status_raw!r}"
            )
        status = cast(Status, status_raw)

        file_raw = probe.get("file")
        file = file_raw if isinstance(file_raw, str) else ""
        case_raw = probe.get("case")
        case = case_raw if isinstance(case_raw, str) else ""
        message_raw = (
            probe.get("message") or probe.get("failure") or probe.get("detail")
        )
        message = message_raw if isinstance(message_raw, str) else ""
        duration_ms = _manifest_duration(probe.get("duration_ms"), path, name)

        cases.append(
            TestCase(
                name=name,
                file=file,
                case=case,
                status=status,
                duration_ms=duration_ms,
                message=message,
            )
        )
    return cases


def _manifest_duration(value: object, path: Path, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReportError(f"{path}: probe {name!r} has a non-numeric duration_ms")
    if value < 0:
        raise ReportError(f"{path}: probe {name!r} has a negative duration_ms")
    return float(value)


# ---------------------------------------------------------------------------
# Layer assembly
# ---------------------------------------------------------------------------


def build_report(
    artifacts_dir: Path,
    *,
    branch: str,
    command: str,
    generated_at: str,
    notes: Sequence[str] = (),
    commit: str = "",
    ci: CiSummary | None = None,
    matrix_refs: dict[tuple[str, str | None], list[MatrixRef]] | None = None,
) -> Report:
    layers = [
        _junit_layer(
            "backend-unit",
            _BACKEND_UNIT,
            artifacts_dir,
            locator_style="pytest",
            head=commit,
        ),
        _junit_layer(
            "backend-integration",
            _BACKEND_INTEGRATION,
            artifacts_dir,
            locator_style="pytest",
            head=commit,
        ),
        _junit_layer(
            "backend-e2e",
            _BACKEND_E2E,
            artifacts_dir,
            locator_style="pytest",
            head=commit,
        ),
        _junit_layer(
            "frontend-unit",
            _FRONTEND_UNIT,
            artifacts_dir,
            locator_style="node",
            head=commit,
        ),
    ]
    layers.extend(_playwright_layers(artifacts_dir, head=commit))
    layers.append(_exploration_layer(artifacts_dir, head=commit))
    return Report(
        branch=branch,
        command=command,
        generated_at=generated_at,
        layers=layers,
        notes=list(notes),
        artifacts_dir=artifacts_dir,
        commit=commit,
        ci=ci,
        matrix_refs=matrix_refs or {},
    )


def _junit_layer(
    key: str,
    artifact: str,
    artifacts_dir: Path,
    *,
    locator_style: LocatorStyle,
    head: str = "",
) -> LayerReport:
    path = artifacts_dir / artifact
    if not path.is_file():
        return LayerReport(
            key=key,
            title=_LAYER_TITLES[key],
            artifact=artifact,
            missing=True,
        )
    return LayerReport(
        key=key,
        title=_LAYER_TITLES[key],
        artifact=artifact,
        cases=parse_junit(path, locator_style=locator_style),
        provenance=load_provenance(path, head=head),
    )


def _playwright_layers(artifacts_dir: Path, *, head: str = "") -> list[LayerReport]:
    path = artifacts_dir / _PLAYWRIGHT
    if not path.is_file():
        return [
            LayerReport(
                key="playwright",
                title=_LAYER_TITLES["playwright"],
                artifact=_PLAYWRIGHT,
                missing=True,
            ),
            LayerReport(
                key="pixel",
                title=_LAYER_TITLES["pixel"],
                artifact=_PLAYWRIGHT,
                missing=True,
            ),
        ]

    all_cases = parse_playwright(path)
    battery = [case for case in all_cases if not is_visual_spec(case.file)]
    pixel = [case for case in all_cases if is_visual_spec(case.file)]
    provenance = load_provenance(path, head=head)
    return [
        LayerReport(
            key="playwright",
            title=_LAYER_TITLES["playwright"],
            artifact=f"{_PLAYWRIGHT}（非 visual 用例）",
            cases=battery,
            provenance=provenance,
        ),
        LayerReport(
            key="pixel",
            title=_LAYER_TITLES["pixel"],
            artifact=f"{_PLAYWRIGHT}（visual 用例）",
            cases=pixel,
            provenance=provenance,
        ),
    ]


def _exploration_layer(artifacts_dir: Path, *, head: str = "") -> LayerReport:
    path = artifacts_dir / _EXPLORATION
    if not path.is_file():
        return LayerReport(
            key="exploration",
            title=_LAYER_TITLES["exploration"],
            artifact=_EXPLORATION,
            missing=True,
        )
    return LayerReport(
        key="exploration",
        title=_LAYER_TITLES["exploration"],
        artifact=_EXPLORATION,
        cases=parse_exploration(path),
        provenance=load_provenance(path, head=head),
    )


def display_cases(layer: LayerReport) -> list[TestCase]:
    """A missing layer becomes one honest dashed placeholder square."""
    if not layer.missing:
        return layer.cases
    return [
        TestCase(
            name=f"{layer.title}：未运行",
            file=layer.artifact,
            case="",
            status="not_run",
            message=f"本轮没有找到结果文件 {layer.artifact}。",
        )
    ]


def total_counts(report: Report) -> dict[Status, int]:
    totals: dict[Status, int] = dict.fromkeys(_STATUSES, 0)
    for layer in report.layers:
        for case in display_cases(layer):
            totals[case.status] += 1
    return totals


def provenance_summary(report: Report) -> dict[ProvenanceState, int]:
    """Per-state counts over the layers that HAVE an artifact — mixed
    runs stay visible as their own composition, never one verdict."""
    counts: dict[ProvenanceState, int] = {}
    for layer in report.layers:
        if layer.provenance is None:
            continue
        counts[layer.provenance.state] = counts.get(layer.provenance.state, 0) + 1
    return counts


def layer_summary(layer: LayerReport) -> str:
    if layer.missing:
        return "未运行"
    cases = layer.cases
    if not cases:
        return "0 个"
    counts: Counter[Status] = Counter(case.status for case in cases)
    failed = counts["failed"]
    skipped = counts["skipped"]
    not_run = counts["not_run"]
    if failed == 0 and skipped == 0 and not_run == 0:
        return f"{len(cases)} 个 · 全绿"

    parts = [f"{len(cases)} 个"]
    if failed:
        parts.append(f"{failed} 红")
    if counts["passed"]:
        parts.append(f"{counts['passed']} 绿")
    if skipped:
        parts.append(f"{skipped} 灰")
    if not_run:
        parts.append(f"{not_run} 未运行")
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------


def render_html(report: Report) -> str:
    totals = total_counts(report)
    entries: list[dict[str, str]] = []
    sections: list[str] = []
    for index, layer in enumerate(report.layers, start=1):
        buttons = []
        for case in display_cases(layer):
            entry_index = len(entries)
            entries.append(_entry(layer, case, report.matrix_refs))
            buttons.append(_square(entry_index, case))
        sections.append(_layer_section(index, layer, buttons))

    header = _header(report, totals)
    notes = _notes(report.notes)
    payload = _json_for_script(entries)
    footer = (
        "<footer>由 backend/scripts/test_report.py 生成 · 离线可用 · "
        f"结果文件目录：<code>{_esc(report.artifacts_dir)}</code></footer>"
    )
    return (
        "<!doctype html>\n"
        '<html lang="zh-CN">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>CampusQuest 测试总览</title>\n"
        f"<style>{_STYLE}</style>\n"
        "</head>\n<body>\n"
        '<div class="wrap">\n'
        f"{header}\n"
        f"{notes}\n"
        "<main>\n" + "\n".join(sections) + "\n</main>\n"
        f"{_DETAIL}\n"
        f"{footer}\n"
        "</div>\n"
        f'<script type="application/json" id="report-data">{payload}</script>\n'
        f"<script>{_SCRIPT}</script>\n"
        "</body>\n</html>\n"
    )


def _entry(
    layer: LayerReport,
    case: TestCase,
    refs: dict[tuple[str, str | None], list[MatrixRef]],
) -> dict[str, str]:
    entry = {
        "name": case.name,
        "layer": layer.title,
        "locator": case.locator,
        "status": case.status,
        "duration": _human_duration(case.duration_ms),
        "message": case.message,
    }
    match = refs.get((case.file, case.case)) or refs.get((case.file, None))
    if match:
        ref = match[0]
        entry["matrix"] = f"{ref.matrix} · {ref.row_id}"
        entry["expectation"] = ref.expectation
    return entry


def _header(report: Report, totals: dict[Status, int]) -> str:
    passed = totals["passed"]
    failed = totals["failed"]
    skipped = totals["skipped"]
    not_run = totals["not_run"]
    commit = report.commit[:7] if report.commit else "unknown"
    prov = provenance_summary(report)
    if prov:
        prov_chips = " · ".join(
            f"{count} {PROVENANCE_LABELS[state]}"
            for state, count in sorted(prov.items())
        )
        prov_html = (
            '<div class="prov-strip">结果文件来源（对照本报告 commit）：'
            f"{_esc(prov_chips)}</div>"
        )
    else:
        prov_html = ""
    return (
        "<header>\n"
        "<h1>测试总览</h1>\n"
        '<p class="subtitle">离线静态报告 · 每个方块是一个测试；'
        "点击方块查看详情，悬停显示测试名。</p>\n"
        '<div class="run-meta">'
        f"<span>提交 <code>{_esc(commit)}</code></span>"
        f"<span>分支 <code>{_esc(report.branch)}</code></span>"
        f"<span>生成命令 <code>{_esc(report.command)}</code></span>"
        f"<span>生成时间 {_esc(report.generated_at)}</span>"
        "</div>\n"
        f"{prov_html}\n"
        f"{_ci_strip(report.ci)}\n"
        '<p class="totals">总计 '
        f'<b class="ok">{passed} 绿</b> / '
        f'<b class="bad">{failed} 红</b> / '
        f'<b class="muted">{skipped} 灰</b> / '
        f'<b class="miss">{not_run} 未运行</b></p>\n'
        '<ul class="legend">'
        '<li><span class="swatch status-passed"></span> 通过</li>'
        '<li><span class="swatch status-failed"></span> 失败</li>'
        '<li><span class="swatch status-skipped"></span> 合法跳过</li>'
        '<li><span class="swatch status-not_run"></span> '
        "未运行（没有结果文件）</li>"
        "</ul>\n"
        "</header>"
    )


_CI_LABELS = {
    "Backend (Python 3.12)": "后端",
    "Frontend (Node 22)": "前端",
    "Pixel visual regression (Playwright)": "像素",
}


def _ci_strip(ci: CiSummary | None) -> str:
    """The commit's CI verdict as one strip of chips (or a neutral note)."""
    if ci is None:
        return (
            '<div class="ci ci-none">CI 未记录（本地生成，未关联 GitHub Actions）</div>'
        )
    if ci.status == "none":
        return '<div class="ci ci-none">该 commit 无 CI 记录</div>'
    if ci.status == "unavailable":
        reason = f"：{_esc(ci.error)}" if ci.error else ""
        return f'<div class="ci ci-unavailable">CI 不可用{reason}</div>'
    chips = []
    for job in ci.jobs:
        label = _CI_LABELS.get(job.name, job.name)
        chips.append(
            f'<span class="ci-chip ci-{_esc(job.state)}" title="{_esc(job.name)}">'
            f"{_esc(label)}</span>"
        )
    link = ""
    if ci.run_url:
        label = (
            "查看运行"
            if ci.status != "completed"
            else f"CI {_esc(ci.conclusion or ci.status)}"
        )
        link = (
            f'<a class="ci-link" href="{_esc(ci.run_url)}" target="_blank" '
            f'rel="noreferrer">{label}</a>'
        )
    return (
        '<div class="ci"><span class="ci-title">CI</span>'
        + "".join(chips)
        + link
        + "</div>"
    )


def _notes(notes: Sequence[str]) -> str:
    if not notes:
        return ""
    items = "".join(f"<li>{_esc(note)}</li>" for note in notes)
    return f'<section class="notes"><h2>注意事项</h2><ul>{items}</ul></section>'


def _layer_section(index: int, layer: LayerReport, buttons: list[str]) -> str:
    if buttons:
        squares = '<div class="squares">' + "".join(buttons) + "</div>"
    else:
        squares = '<p class="empty">结果文件存在，但没有用例</p>'
    label = "期望结果文件" if layer.missing else "来源"
    prov_chip = ""
    if layer.provenance is not None:
        provenance = layer.provenance
        tip = (
            f"source {provenance.source_sha[:7] or '—'} · "
            f"{provenance.command} · {provenance.generated_at_utc or '—'}"
        )
        prov_chip = (
            f'<span class="prov prov-{provenance.state}" '
            f'title="{_esc(tip)}">{_esc(PROVENANCE_LABELS[provenance.state])}</span>'
        )
    return (
        f'<section class="layer" aria-labelledby="layer-{index}-title">'
        '<div class="layer-head">'
        f'<span class="layer-index">{index}</span>'
        f'<h2 id="layer-{index}-title">{_esc(layer.title)}</h2>'
        f'<span class="layer-summary">{_esc(layer_summary(layer))}</span>'
        f"{prov_chip}"
        "</div>"
        f"{squares}"
        f'<p class="artifact">{label}：<code>{_esc(layer.artifact)}</code></p>'
        "</section>"
    )


def _square(index: int, case: TestCase) -> str:
    label = f"{STATUS_LABELS[case.status]}：{case.name}"
    return (
        f'<button type="button" class="square status-{case.status}" '
        f'data-index="{index}" title="{_esc(case.name)}" '
        f'aria-label="{_esc(label)}"></button>'
    )


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _json_for_script(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    # ``</script>`` inside a JSON string would close the block early.
    # Replacing the angle brackets keeps the JSON text valid and inert.
    return raw.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _human_duration(ms: float | None) -> str:
    if ms is None:
        return "—"
    if ms < 1000:
        return f"{ms:.0f} ms"
    seconds = ms / 1000.0
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes = int(seconds // 60)
    return f"{minutes} 分 {seconds - minutes * 60:.0f} 秒"


_DETAIL = (
    '<aside id="detail" class="detail" tabindex="-1" aria-live="polite" hidden>\n'
    '<div class="detail-head"><h2>测试详情</h2>'
    '<button type="button" id="detail-close">关闭</button></div>\n'
    "<dl>"
    '<dt>测试名</dt><dd id="detail-name"></dd>'
    '<dt>所在层</dt><dd id="detail-layer"></dd>'
    '<dt>文件::用例</dt><dd id="detail-locator" class="mono"></dd>'
    '<dt>状态</dt><dd id="detail-status"></dd>'
    '<dt>耗时</dt><dd id="detail-duration"></dd>'
    "</dl>\n"
    '<div id="detail-matrix" class="matrix-block" hidden>'
    '<h3>规约预期 <span id="detail-matrix-row" class="mono"></span></h3>'
    '<p id="detail-matrix-text"></p></div>\n'
    '<div id="detail-failure"><h3>失败信息</h3>'
    '<pre id="detail-message"></pre></div>\n'
    "</aside>"
)

_STYLE = """
:root {
  --bg: #f4f5f7;
  --panel: #ffffff;
  --text: #18181b;
  --muted: #52525b;
  --faint: #a1a1aa;
  --border: #e4e4e7;
  --green: #22c55e;
  --green-text: #15803d;
  --red: #ef4444;
  --red-text: #b91c1c;
  --grey: #c4c4c8;
  --focus: #2563eb;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto,
    "Noto Sans SC", "Helvetica Neue", Arial, sans-serif;
  font-size: 14px;
  line-height: 1.55;
}
.wrap { width: 100%; max-width: 1440px; margin: 0 auto; padding: 24px; }
header, .notes, .layer, .detail, footer {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 16px 18px;
  margin-bottom: 14px;
}
h1 { margin: 0 0 4px; font-size: 22px; }
h2 { margin: 0; font-size: 16px; }
h3 { margin: 14px 0 6px; font-size: 14px; }
.subtitle, .artifact, footer, .empty { color: var(--muted); }
.subtitle { margin: 0 0 12px; font-size: 13px; }
.run-meta { display: flex; flex-wrap: wrap; gap: 6px 20px; font-size: 13px; }
.ci { display: flex; flex-wrap: wrap; align-items: center; gap: 6px;
      margin-top: 8px; font-size: 13px; }
.ci-title { color: var(--muted); }
.ci-chip { padding: 1px 9px; border-radius: 999px; color: #fff; font-size: 12px; }
.ci-success { background: var(--green); }
.ci-failure, .ci-timed_out { background: var(--red); }
.ci-pending { background: #d97706; }
.ci-neutral, .ci-skipped, .ci-cancelled { background: #71717a; }
.ci-none, .ci-unavailable { color: var(--muted); }
.ci-link { color: var(--focus); text-decoration: none; }
.ci-link:hover { text-decoration: underline; }
code, .mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas,
    "Liberation Mono", monospace;
  overflow-wrap: anywhere;
}
.run-meta code { background: #f4f4f5; border-radius: 4px; padding: 1px 5px; }
.prov-strip { color: var(--muted); font-size: 13px; margin-top: 6px; }
.prov { margin-left: 4px; padding: 0 8px; border-radius: 999px; font-size: 12px;
        border: 1px solid var(--border); color: var(--muted); }
.prov-fresh { border-color: var(--green); color: var(--green-text); }
.prov-dirty { border-color: #d97706; color: #b45309; }
.prov-stale { border-color: var(--red); color: var(--red-text); }
.totals { margin: 12px 0 8px; font-size: 15px; }
.totals .ok { color: var(--green-text); }
.totals .bad { color: var(--red-text); }
.totals .muted { color: var(--muted); }
.totals .miss { color: #6b7280; }
.legend { display: flex; flex-wrap: wrap; gap: 6px 16px; margin: 0;
  padding: 0; list-style: none; font-size: 13px; color: var(--muted); }
.legend li { display: flex; align-items: center; gap: 6px; }
.swatch { width: 12px; height: 12px; border-radius: 3px; display: inline-block; }
.notes { border-left: 3px solid #f59e0b; }
.notes h2 { font-size: 14px; margin-bottom: 6px; }
.notes ul { margin: 0; padding-left: 20px; }
.layer-head { display: flex; flex-wrap: wrap; align-items: baseline;
  gap: 4px 10px; margin-bottom: 10px; }
.layer-index { color: var(--faint); font-variant-numeric: tabular-nums; }
.layer-summary { color: var(--muted); font-size: 13px; }
.squares { display: flex; flex-wrap: wrap; gap: 4px; min-width: 0; }
.square {
  width: 15px; height: 15px; padding: 0; border: 0; border-radius: 3px;
  cursor: pointer; flex: 0 0 auto;
}
.square:hover { filter: brightness(0.9); }
.square:focus-visible, #detail-close:focus-visible {
  outline: 2px solid var(--focus); outline-offset: 2px;
}
.status-passed { background: var(--green); }
.status-failed { background: var(--red); outline: 2px solid var(--red-text);
  outline-offset: -1px; }
.status-skipped { background: var(--grey); }
.status-not_run { background: transparent; border: 1px dashed #9ca3af; }
.detail { border-left: 3px solid var(--focus); }
.detail:focus { outline: 2px solid var(--focus); outline-offset: 2px; }
.detail-head { display: flex; flex-wrap: wrap; align-items: center;
  justify-content: space-between; gap: 8px; }
.detail dl { display: grid; grid-template-columns: max-content minmax(0, 1fr);
  gap: 6px 14px; margin: 12px 0 0; }
.detail dt { color: var(--muted); }
.detail dd { margin: 0; overflow-wrap: anywhere; }
.matrix-block { margin-top: 12px; padding: 10px 12px; background: #fafaf9;
      border: 1px solid var(--border); border-radius: 8px; }
.matrix-block h3 { margin: 0 0 4px; }
.matrix-block .mono { color: var(--muted); font-weight: 400; }
.matrix-block p { margin: 0; color: var(--text); }
.detail-status-passed { color: var(--green-text); }
.detail-status-failed { color: var(--red-text); }
.detail-status-skipped, .detail-status-not_run { color: var(--muted); }
.detail pre {
  margin: 0; padding: 10px 12px; background: #18181b; color: #f4f4f5;
  border-radius: 8px; white-space: pre-wrap; overflow-wrap: anywhere;
}
#detail-close { border: 1px solid var(--border); background: #fff;
  border-radius: 6px; padding: 3px 10px; cursor: pointer; }
footer { font-size: 12px; }
[hidden] { display: none !important; }
@media (max-width: 720px) {
  .wrap { padding: 10px; }
  header, .notes, .layer, .detail, footer { padding: 13px 13px; }
  .detail dl { grid-template-columns: minmax(0, 1fr); gap: 2px; }
  .detail dd { margin-bottom: 6px; }
}
"""

_SCRIPT = """
(() => {
  const entries = JSON.parse(
    document.getElementById("report-data").textContent
  );
  const panel = document.getElementById("detail");
  const close = document.getElementById("detail-close");
  const failure = document.getElementById("detail-failure");
  const fields = {
    name: document.getElementById("detail-name"),
    layer: document.getElementById("detail-layer"),
    locator: document.getElementById("detail-locator"),
    status: document.getElementById("detail-status"),
    duration: document.getElementById("detail-duration"),
    message: document.getElementById("detail-message"),
    matrixBlock: document.getElementById("detail-matrix"),
    matrixRow: document.getElementById("detail-matrix-row"),
    matrixText: document.getElementById("detail-matrix-text"),
  };
  const labels = {
    passed: "通过",
    failed: "失败",
    skipped: "合法跳过",
    not_run: "未运行",
  };
  function show(index) {
    const entry = entries[index];
    if (!entry) return;
    fields.name.textContent = entry.name;
    fields.layer.textContent = entry.layer;
    fields.locator.textContent = entry.locator || "—";
    fields.status.textContent = labels[entry.status] || entry.status;
    fields.status.className = "detail-status-" + entry.status;
    fields.duration.textContent = entry.duration;
    fields.message.textContent = entry.message || "";
    if (entry.matrix) {
      fields.matrixRow.textContent = entry.matrix;
      fields.matrixText.textContent = entry.expectation || "";
      fields.matrixBlock.hidden = false;
    } else {
      fields.matrixBlock.hidden = true;
    }
    failure.hidden = !entry.message;
    panel.hidden = false;
    panel.focus();
  }
  document.querySelectorAll(".square").forEach((square) => {
    square.addEventListener("click", () => {
      show(Number(square.dataset.index));
    });
  });
  close.addEventListener("click", () => {
    panel.hidden = true;
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !panel.hidden) {
      panel.hidden = true;
    }
  });
})();
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def default_generated_at() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def detect_branch(repo_root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if completed.returncode != 0:
        return "unknown"
    branch = completed.stdout.strip()
    return branch or "unknown"


def detect_commit(repo_root: Path) -> str:
    """HEAD sha, or "" when git is unavailable (tests, tarballs)."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def detect_dirty(repo_root: Path) -> bool:
    """Whether the workspace has uncommitted changes — fail-closed:
    when git cannot answer, the tree cannot be proven clean."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return True
    if result.returncode != 0:
        return True
    return bool(result.stdout.strip())


def utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _job_state(status: str, conclusion: str | None) -> str:
    if conclusion in {"success", "failure", "cancelled", "skipped", "timed_out"}:
        return conclusion
    if status in {"queued", "in_progress", "waiting", "requested"}:
        return "pending"
    return "neutral"


def load_ci(path: Path) -> CiSummary:
    """Read the ci_record JSON; unknown shapes degrade to a neutral record."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    jobs = tuple(
        CiJobSummary(
            name=str(job.get("name", "")),
            state=_job_state(str(job.get("status", "")), job.get("conclusion")),
        )
        for job in payload.get("jobs", [])
        if isinstance(job, dict)
    )
    return CiSummary(
        commit=str(payload.get("commit", "")),
        status=str(payload.get("status", "unknown")),
        conclusion=payload.get("conclusion"),
        run_url=payload.get("run_url"),
        jobs=jobs,
        error=payload.get("error"),
    )


def _history_entries(history_dir: Path) -> list[tuple[str, Path, dict[str, Any]]]:
    entries: list[tuple[str, Path, dict[str, Any]]] = []
    if not history_dir.exists():
        return entries
    for directory in history_dir.iterdir():
        if not directory.is_dir():
            continue
        meta_path = directory / "meta.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        entries.append((str(meta.get("generated_at", "")), directory, meta))
    entries.sort(key=lambda item: item[0], reverse=True)
    return entries


def _prune_history(history_dir: Path, keep: int) -> None:
    for _stamp, directory, _meta in _history_entries(history_dir)[keep:]:
        shutil.rmtree(directory, ignore_errors=True)


def _write_history_index(history_dir: Path) -> None:
    rows: list[str] = []
    for stamp, directory, meta in _history_entries(history_dir):
        short = _esc(str(meta.get("short_commit") or directory.name))
        branch = _esc(str(meta.get("branch", "")))
        totals_raw = meta.get("totals")
        totals = totals_raw if isinstance(totals_raw, dict) else {}
        ci_raw = meta.get("ci")
        ci = ci_raw if isinstance(ci_raw, dict) else {}
        ci_label = _esc(str(ci.get("conclusion") or ci.get("status") or "无记录"))
        rows.append(
            "<li>"
            f'<a href="{directory.name}/test-report.html"><code>{short}</code></a>'
            f"<span>{branch}</span>"
            f"<span>{_esc(stamp)}</span>"
            f'<span class="ok">{int(totals.get("passed", 0))} 绿</span>'
            f'<span class="bad">{int(totals.get("failed", 0))} 红</span>'
            f"<span>CI {ci_label}</span>"
            "</li>"
        )
    document = (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>测试历史</title><style>"
        "body{font:14px/1.6 system-ui,sans-serif;background:#f4f5f7;color:#18181b;"
        "margin:0;padding:24px}main{max-width:960px;margin:0 auto;background:#fff;"
        "border:1px solid #e4e4e7;border-radius:10px;padding:20px}"
        "h1{font-size:20px;margin:0 0 12px}ul{list-style:none;padding:0;margin:0}"
        "li{display:flex;flex-wrap:wrap;gap:6px 16px;align-items:baseline;"
        "padding:8px 0;border-bottom:1px solid #f0f0f1}"
        "a{color:#2563eb;text-decoration:none}.ok{color:#15803d}.bad{color:#b91c1c}"
        "code{background:#f4f4f5;border-radius:4px;padding:1px 5px}"
        "</style></head><body><main><h1>测试历史</h1>"
        '<p><a href="../test-report.html">最新报告</a> · '
        '<a href="../index.html">首页</a></p>'
        "<ul>"
        + ("".join(rows) or "<li>还没有历史记录</li>")
        + "</ul></main></body></html>\n"
    )
    history_dir.mkdir(parents=True, exist_ok=True)
    (history_dir / "index.html").write_text(document, encoding="utf-8")


def _write_landing(
    reports_dir: Path, report: Report, totals: dict[Status, int]
) -> None:
    short = _esc(report.commit[:7] if report.commit else "unknown")
    ci = report.ci
    if ci is None:
        ci_label = "未记录"
    elif ci.status == "none":
        ci_label = "无记录"
    elif ci.status == "unavailable":
        ci_label = "不可用"
    else:
        ci_label = _esc(ci.conclusion or ci.status)
    document = (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>CampusQuest 测试总览</title><style>"
        "body{font:14px/1.7 system-ui,sans-serif;background:#f4f5f7;color:#18181b;"
        "margin:0;padding:32px}main{max-width:720px;margin:0 auto;background:#fff;"
        "border:1px solid #e4e4e7;border-radius:10px;padding:24px}"
        "h1{font-size:22px;margin:0 0 8px}p{color:#52525b}"
        "a{color:#2563eb;font-size:15px}"
        "code{background:#f4f4f5;border-radius:4px;padding:1px 5px}"
        ".ok{color:#15803d}.bad{color:#b91c1c}"
        "</style></head><body><main><h1>测试总览</h1>"
        f"<p>最新提交 <code>{short}</code> · 分支 <code>{_esc(report.branch)}</code>"
        f" · CI {ci_label} · {_esc(report.generated_at)}</p>"
        f'<p><b class="ok">{totals["passed"]} 绿</b> / '
        f'<b class="bad">{totals["failed"]} 红</b> / '
        f"{totals['skipped']} 灰 / {totals['not_run']} 未运行</p>"
        '<p><a href="test-report.html">打开最新报告</a> · '
        '<a href="history/index.html">历史记录</a></p>'
        "</main></body></html>\n"
    )
    (reports_dir / "index.html").write_text(document, encoding="utf-8")


def _archive_history(
    *,
    output: Path,
    report: Report,
    totals: dict[Status, int],
    history_dir: Path,
    keep: int,
) -> None:
    short = report.commit[:7] if report.commit else "unknown"
    destination = history_dir / short
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(output, destination / "test-report.html")
    meta = {
        "commit": report.commit,
        "short_commit": short,
        "branch": report.branch,
        "generated_at": report.generated_at,
        "command": report.command,
        "totals": dict(totals),
        "artifact_provenance": {
            layer.key: {
                "state": layer.provenance.state,
                "source_sha7": layer.provenance.source_sha[:7],
                "command": layer.provenance.command,
                "generated_at_utc": layer.provenance.generated_at_utc,
            }
            for layer in report.layers
            if layer.provenance is not None
        },
        "ci": None
        if report.ci is None
        else {
            "status": report.ci.status,
            "conclusion": report.ci.conclusion,
            "run_url": report.ci.run_url,
        },
    }
    (destination / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    _prune_history(history_dir, keep)
    _write_history_index(history_dir)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the offline CampusQuest test dashboard."
    )
    parser.add_argument(
        "--artifacts-dir",
        type=Path,
        default=None,
        help="directory holding the result files (default: reports/artifacts)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="HTML output path (default: reports/test-report.html)",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="repository root used for defaults and branch detection",
    )
    parser.add_argument("--branch", default=None, help="branch label override")
    parser.add_argument(
        "--commit", default=None, help="commit sha shown in the header and history"
    )
    parser.add_argument(
        "--ci-file", type=Path, default=None, help="ci_record JSON for this commit"
    )
    parser.add_argument(
        "--history-dir",
        type=Path,
        default=None,
        help="per-commit archive directory (default: reports/history)",
    )
    parser.add_argument(
        "--matrix-dir",
        type=Path,
        default=None,
        help="spec-test matrix directory for detail-card expectations "
        "(default: <repo-root>/docs/quality/test-matrix)",
    )
    parser.add_argument(
        "--keep-history",
        type=int,
        default=20,
        help="how many per-commit archives to keep (default: 20)",
    )
    parser.add_argument(
        "--command",
        default="make test-report",
        help="command shown in the report header (and recorded in a "
        "stamped artifact's provenance)",
    )
    parser.add_argument(
        "--stamp-artifact",
        type=Path,
        default=None,
        help="write <artifact>.provenance.json next to PATH recording its "
        "source commit, workspace state, command, and content hash, then "
        "exit (no report is generated)",
    )
    parser.add_argument(
        "--generated-at",
        default=None,
        help="timestamp override (tests/reproducible runs)",
    )
    parser.add_argument(
        "--note",
        action="append",
        default=[],
        help="operator note shown in the report (repeatable)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    repo_root: Path = args.repo_root or Path(__file__).resolve().parents[2]

    if args.stamp_artifact is not None:
        artifact: Path = args.stamp_artifact
        if not artifact.is_file():
            print(
                f"test-report: cannot stamp missing artifact {artifact}",
                file=sys.stderr,
            )
            return 1
        payload = stamp_artifact(
            artifact,
            source_sha=detect_commit(repo_root),
            dirty=detect_dirty(repo_root),
            command=args.command,
            generated_at_utc=utc_now_iso(),
        )
        print(f"stamped {sidecar_path(artifact)}")
        print(
            f"  source {payload['source_sha'][:7] or 'unknown'} · "
            f"{'dirty' if payload['dirty'] else 'clean'} · "
            f"sha256 {str(payload['artifact_sha256'])[:12]}…"
        )
        return 0

    artifacts_dir: Path = args.artifacts_dir or repo_root / "reports" / "artifacts"
    output: Path = args.output or repo_root / "reports" / "test-report.html"
    branch: str = args.branch or detect_branch(repo_root)
    generated_at: str = args.generated_at or default_generated_at()
    commit: str = args.commit if args.commit is not None else detect_commit(repo_root)
    notes = [
        note.strip() for note in args.note if isinstance(note, str) and note.strip()
    ]
    matrix_dir: Path = args.matrix_dir or repo_root / "docs" / "quality" / "test-matrix"
    matrix_refs = parse_matrices(matrix_dir)
    ci: CiSummary | None = None
    if args.ci_file is not None:
        try:
            ci = load_ci(args.ci_file)
        except (OSError, ValueError) as exc:
            notes.append(f"CI 记录读取失败：{exc}")
    command: str = args.command

    try:
        report = build_report(
            artifacts_dir,
            branch=branch,
            command=command,
            generated_at=generated_at,
            notes=notes,
            commit=commit,
            ci=ci,
            matrix_refs=matrix_refs,
        )
        document = render_html(report)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(document, encoding="utf-8")
    except (OSError, ReportError) as exc:
        print(f"test-report: {exc}", file=sys.stderr)
        return 1

    totals = total_counts(report)
    history_dir: Path = args.history_dir or repo_root / "reports" / "history"
    try:
        _archive_history(
            output=output,
            report=report,
            totals=totals,
            history_dir=history_dir,
            keep=max(1, args.keep_history),
        )
        _write_landing(output.parent, report, totals)
    except OSError as exc:
        print(f"test-report: history archive failed: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {output}")
    print(
        f"  {totals['passed']} passed, {totals['failed']} failed, "
        f"{totals['skipped']} skipped, {totals['not_run']} not run"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
