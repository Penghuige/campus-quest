# backend/scripts/coverage_ratchet.py
"""Compare current per-module branch coverage against the ratchet file.

The ratchet (owner-approved 2026-10-07): ``coverage-ratchet.json`` pins
one branch-coverage percentage per top-level module of ``app/``. The
gate FAILS when any module drops below its pinned value — improvements
never auto-lower anything, and a raise requires running with
``--update`` (a deliberate, reviewable commit of the new floor).

Inputs: a COMBINED ``.coverage`` data file produced by the three suites
(unit + integration + workers) each run with ``--cov``; the script
reads ``coverage json`` output, never re-runs tests.

Usage:
    uv run python scripts/coverage_ratchet.py            # compare (gate)
    uv run python scripts/coverage_ratchet.py --update   # rewrite floors

Exit codes: 0 pass / 1 regression or untracked module / 2 usage error.
A module missing from the ratchet is a failure too — new code must
enter the ratchet explicitly via --update in the same PR that adds it.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_RATCHET_PATH = _BACKEND_ROOT / "coverage-ratchet.json"

#: Cross-environment measurement noise (reviewer ruling 2026-10-07): the
#: same test set measured on the local stack vs the CI runner drifted
#: 0.1pp on one module (identity 90.8 local vs 90.7 CI — a single
#: environment-conditional branch). A zero-epsilon ratchet would flap on
#: that noise forever; 0.15 covers the observed drift with margin while
#: staying far below any real regression scale. Floors still mean what
#: they say: the CI-observed value, not floor+epsilon. A related ruling
#: (2026-10-08, the community 95.1->94.9 calibration): a floor the CI
#: can only SOMETIMES meet is broken by construction — floors are the
#: CI-reproducible minimum, and single-module drift on identical code
#: is a coverage flake to hunt, never a reason to widen epsilon.
#: Cross-environment MARGIN convention (reviewer ruling 2026-10-17, the
#: first post-B-item CI measurement): community measured 97.3 local vs
#: 96.9 CI on identical code — every OTHER module matched to the digit,
#: the PR #36 single-module coverage-flake signature (an order-sensitive
#: branch CI's random order only sometimes exercises). A flake that
#: exceeds epsilon is NOT hunted under a red main: the floor drops to
#: min(local, CI) - epsilon (97.3/96.9 -> 96.7) in the same PR, the
#: flake-hunt itself is a separate item, and the floor re-tightens to
#: the deterministic value once that hunt lands.
_EPSILON = 0.15

#: Researched residuals waived from the cross-talk guard (rental-style,
#: like the import-linter waivers): the exact out-of-range endpoint set
#: of a KNOWN coverage-limitation fingerprint. Cross-talk is inherently
#: MISFILED: the same fingerprint landed in upload_service.py locally
#: and validation_service.py on CI (#101, 2026-10-17) — the waiver
#: matches the arc fingerprint on ANY host file (the recorded host is
#: provenance, not a match key). Any drift (one new arc, a different
#: line) falls through and fails the gate again, so the list cannot
#: silently rot into a global pass.
#:
#: WAIVER POLICY (owner-approved 2026-10-17, the saga's closing ruling):
#: the greenlet cross-talk is a timing-shaped defect in coverage's own
#: greenlet patch — sysmon/ctrace both reproduce it and 7.16.2 is the
#: latest release, so the configuration layer has no fix (the
#: three-way falsification lives in the B-item report). The waivers ARE
#: the long-term defense: a NEW signature fails the gate red, gets its
#: fingerprint registered here with provenance, and the CI artifact
#: (coverage-diagnosis.bin, uploaded always) carries the arcs for the
#: analysis. REVALIDATION CLAUSE: every coverage version bump (lock
#: update) re-checks both registered signatures — if one stops
#: appearing, retire its waiver entry and file the upstream report with
#: the material already collected (the PR trail holds the six-shape
#: reproduction matrix and the arc anatomy).
_CROSSTALK_WAIVERS: tuple[dict[str, object], ...] = (
    {
        "beyond": frozenset({1910, 1971, 1988}),
        "seen_in": (
            "app/modules/submissions/upload_service.py",
            "app/modules/submissions/validation_service.py",
        ),
        "reason": (
            "coverage's greenlet patch still leaks this three-arc "
            "signature in full-suite combination runs even with "
            "concurrency declared (B item, 2026-10-17; resurfaced on a "
            "different host in #101 CI) — the anchor line stays "
            "covered, the cost is at most one partial branch"
        ),
    },
    {
        # The second signature, a WORKERS-job host (main 4ecd77d CI,
        # 2026-10-17): three hosts/two signatures = a widening surface,
        # so the root fix (the eager worker-test execution shape x the
        # tracer's thread/greenlet bookkeeping) is pulled FORWARD from
        # the backlog into its own PR — this waiver unblocks main and
        # retires with that fix.
        "beyond": frozenset({465, 623, 651, 683, 684}),
        "seen_in": ("app/workers/jobs/validate_submission.py",),
        "reason": (
            "the workers-job host of the greenlet cross-talk: the "
            "eager-mode worker tests' execution shape (suspected "
            "thread-pool job runs) leaks a five-arc signature; root "
            "fix in flight (reviewer directive 2026-10-17)"
        ),
    },
)


def _waived(beyond: set[int]) -> bool:
    return any(
        beyond == set(waiver["beyond"])  # type: ignore[arg-type]
        for waiver in _CROSSTALK_WAIVERS
    )


def _undeclared_concurrency_signatures(data_file: str | None = None) -> list[str]:
    """Out-of-range arc endpoints in the raw data — cross-talk evidence.

    When a concurrency library rides outside the tracer's model (the B
    item, 2026-10-17: SQLAlchemy AsyncSession's greenlet bridge with
    ``concurrency`` undeclared), arcs land in this file's data carrying
    line numbers of a DIFFERENT file — endpoints beyond this file's line
    count. The report layer drops those arcs silently and the lost lines
    come back "missing"; this check surfaces the fingerprint so the gate
    fails loudly instead of measuring garbage. Empty result = the data
    is fit to compare against floors.
    """
    from coverage import CoverageData

    data = CoverageData(data_file or str(_BACKEND_ROOT / ".coverage"))
    data.read()
    hits: list[str] = []
    for measured in data.measured_files():
        arcs = data.arcs(measured) or []
        try:
            with open(measured, encoding="utf-8") as handle:
                # An empty module still executes its (empty) code object,
                # whose implicit line 1 is legitimate — floor at 1.
                total_lines = max(1, sum(1 for _ in handle))
        except OSError:
            continue  # source gone (branch switched mid-run) — not evidence
        beyond = sorted(
            {
                endpoint
                for arc in arcs
                for endpoint in (abs(arc[0]), abs(arc[1]))
                if endpoint > total_lines
            }
        )
        if beyond and _waived(set(beyond)):
            print(f"WAIVED cross-talk residual: {measured} {beyond}")
            continue
        if beyond:
            # Print the FULL set: the waivers match by exact fingerprint,
            # and a truncated list hides the extra endpoint that defeats
            # them (#135 CI, 2026-10-23 — the logged five matched a
            # waiver while the comparison still failed).
            hits.append(
                f"{measured}: arc endpoints beyond EOF {beyond} — an "
                "undeclared concurrency library is corrupting the trace "
                "(pyproject [tool.coverage.run] concurrency)"
            )
    return hits


def _coverage_json() -> dict:
    result = subprocess.run(
        ["uv", "run", "coverage", "json", "-o", "-", "--quiet"],
        cwd=_BACKEND_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _module_of(file_path: str) -> str | None:
    parts = file_path.split("/")
    if not parts or parts[0] != "app":
        return None
    # Top-level package under app/ (modules/<name>, core, integrations,
    # workers, db, ...) — the granularity the ratchet pins.
    if parts[1] == "modules" and len(parts) > 2:
        return f"app/modules/{parts[2]}"
    return f"app/{parts[1]}"


def _current_by_module(report: dict) -> dict[str, dict]:
    per_module: dict[str, dict] = {}
    for file_path, file_stats in report["files"].items():
        module = _module_of(file_path)
        if module is None:
            continue
        bucket = per_module.setdefault(
            module,
            {
                "covered": 0,
                "missed": 0,
                "covered_branches": 0,
                "partial_branches": 0,
            },
        )
        summary = file_stats["summary"]
        bucket["covered"] += summary["covered_lines"]
        bucket["missed"] += summary["missing_lines"]
        bucket["covered_branches"] += summary.get("covered_branches", 0)
        bucket["partial_branches"] += summary.get("num_partial_branches", 0)
    return per_module


def _percent(stats: dict) -> float:
    statements = stats["covered"] + stats["missed"]
    branches = stats["covered_branches"] + stats["partial_branches"]
    total = statements + branches
    if total == 0:
        return 100.0
    hit = stats["covered"] + stats["covered_branches"]
    return round(100.0 * hit / total, 1)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update", action="store_true", help="rewrite the ratchet to current levels"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the per-module table as JSON (for PR descriptions)",
    )
    args = parser.parse_args()

    report = _coverage_json()

    # Guard (B item): refuse to measure — or rewrite floors from — data
    # carrying the cross-talk fingerprint. Measuring a corrupted trace
    # is how four phantom-miss incidents shipped (2026-10-17).
    signatures = _undeclared_concurrency_signatures()
    if signatures:
        for signature in signatures[:10]:
            print(f"CROSS-TALK: {signature}")
        print(
            f"\n{len(signatures)} file(s) show arcs beyond EOF — fix the "
            "tracer setup (declared concurrency) and re-run the suites; "
            "this data will not be compared or written to floors."
        )
        return 1

    current = {
        module: _percent(stats) for module, stats in _current_by_module(report).items()
    }

    if args.update:
        _RATCHET_PATH.write_text(
            json.dumps(
                {module: current[module] for module in sorted(current)},
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"ratchet updated: {len(current)} modules -> {_RATCHET_PATH.name}")
        return 0

    ratchet: dict[str, float] = {}
    if _RATCHET_PATH.exists():
        ratchet = json.loads(_RATCHET_PATH.read_text(encoding="utf-8"))

    violations: list[str] = []
    if args.json:
        print(json.dumps(current, indent=2, ensure_ascii=False))
    print(f"{'module':32} {'ratchet':>8} {'now':>8}")
    for module in sorted(set(ratchet) | set(current)):
        floor = ratchet.get(module)
        now = current.get(module)
        if floor is None:
            violations.append(f"{module}: not in the ratchet (run --update)")
            floor_display = "--"
        else:
            floor_display = f"{floor:.1f}"
        now_display = f"{now:.1f}" if now is not None else "--"
        marker = ""
        if floor is not None and now is not None and now < floor - _EPSILON:
            violations.append(
                f"{module}: {now:.1f} < ratchet {floor:.1f} "
                f"(more than the {_EPSILON}pp cross-environment noise "
                "allowance) — restore coverage or raise the floor via "
                "--update"
            )
            marker = "  <- REGRESSION"
        print(f"{module:32} {floor_display:>8} {now_display:>8}{marker}")

    if violations:
        print(f"\n{len(violations)} violation(s):")
        for violation in violations:
            print(f"  - {violation}")
        return 1
    print("\nratchet holds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
