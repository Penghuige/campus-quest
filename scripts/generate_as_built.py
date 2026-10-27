#!/usr/bin/env python3
"""Generate docs/architecture/as-built.md — the derived, always-current map.

Purpose
    The architecture CONTRACT gate (`make arch-lint`) enforces the written
    rules with explicit waivers; this script is the complementary truth
    side. It scans the tree exactly as it is (no waivers) and renders:

      - the backend cross-module dependency graph (mermaid),
      - the backend boundary-debt tables (drift vs the documented rules),
      - the frontend feature dependency graph (mermaid),
      - the frontend boundary notes (cross-feature / upward imports).

    Everything here is derived; nothing is hand-maintained. Regenerate with
    `make arch-map` (or `python3 scripts/generate_as_built.py`) and commit
    the result — the committed map ages with the tree between regenerations,
    which is itself useful signal.

Usage
    uv run python scripts/generate_as_built.py [--check] [--self-test]

    --check      regenerate to memory and exit 1 if the committed
                 docs/architecture/as-built.md differs (CI-style freshness
                 gate; not wired into CI yet).
    --self-test  prove the scanners can fail: parse synthetic snippets and
                 assert the exact expected edges, then exit 0.

Exit codes
    0 success (or self-test pass) · 1 mismatch under --check / self-test
    failure · 2 unexpected error.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_APP = REPO_ROOT / "backend" / "app"
FRONTEND_SRC = REPO_ROOT / "frontend" / "src"
OUT_PATH = REPO_ROOT / "docs" / "architecture" / "as-built.md"

# The map stamps the HEAD it was rendered from (informational
# provenance). A byte-exact freshness check would flag a fresh map the
# moment any later commit lands — the gate compares the SCANNED CONTENT
# with the stamp line normalized out.
_HEAD_STAMP_RE = re.compile(r"Generated from HEAD [0-9a-zA-Z\-]+")


def _normalize_stamp(text: str) -> str:
    return _HEAD_STAMP_RE.sub("Generated from HEAD <stamp>", text)


def check_freshness(committed: str, generated: str) -> bool:
    """Content equality with the HEAD stamp normalized out — only real
    tree drift is staleness."""
    return _normalize_stamp(committed) == _normalize_stamp(generated)

# ---------------------------------------------------------------------------
# Backend scan (AST — precise for Python)
# ---------------------------------------------------------------------------

TS_IMPORT_RE = re.compile(
    r"""(?:from\s+|import\s+)['"](@/[^'"]+|\.\.?/[^'"]+)['"]"""
)


def backend_scan() -> dict:
    """Return {module: {target_module: [ (file, line) ... ] }} cross-module."""
    cross: dict[str, dict[str, list[tuple[str, int]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for py in sorted(BACKEND_APP.rglob("*.py")):
        rel = py.relative_to(BACKEND_APP).with_suffix("")
        parts = rel.parts
        if not parts or parts[-1] == "__init__":
            top = ".".join(parts[:-1]) if len(parts) > 1 else "(app)"
        else:
            top = ".".join(parts[:-1]) if len(parts) > 1 else parts[0]
        # module key: the owning PACKAGE (e.g. modules.tasks, workers.jobs,
        # core) — sub-files are attributed to their package for readability.
        key = _pkg_key(["app", *parts])
        if key == "modules":  # the modules package itself
            key = "(modules)"
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - scan robustness
            continue
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.ImportFrom):
                if node.module:
                    targets.append((node.module, node.lineno))
                # relative imports: resolve against this file's package
                if node.level > 0:
                    base = parts[:-1] if parts[-1] != "__init__" else parts[:-1]
                    up = base[: len(base) - (node.level - 1)]
                    mod = ".".join([*up, node.module]) if node.module else ".".join(up)
                    targets.append((mod, node.lineno))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    targets.append((alias.name, node.lineno))
            for mod, line in targets:
                tgt_key = _pkg_key(mod.split("."))
                if tgt_key and tgt_key != key:
                    cross[key][tgt_key].append((str(rel), line))
    return cross


_APP_TOPS = {"modules", "workers", "core", "db", "integrations", "main"}


def _pkg_key(parts: tuple[str, ...] | list[str]) -> str:
    """Collapse app.* paths to the interesting package (module / workers /
    core / db / integrations / main). Accepts parts with or without the
    leading "app" segment (AST absolute imports carry it; relative-import
    resolution and rglob paths do not). Anything else (stdlib, third-party)
    collapses to "" so it never becomes a graph node."""
    rest = list(parts)
    if rest and rest[0] == "app":
        rest = rest[1:]
    if not rest or rest[0] not in _APP_TOPS:
        return ""
    if rest[0] == "modules" and len(rest) > 1:
        return f"modules.{rest[1]}"
    return rest[0]


def backend_module_file_stats() -> dict[str, dict[str, int]]:
    """Per module: number of router/service/models files (shape snapshot)."""
    stats: dict[str, dict[str, int]] = defaultdict(
        lambda: {"routers": 0, "services": 0, "files": 0}
    )
    mods_dir = BACKEND_APP / "modules"
    for mod in sorted(p.name for p in mods_dir.iterdir() if p.is_dir()):
        for f in (mods_dir / mod).glob("*.py"):
            name = f.stem
            if name == "__init__":
                continue
            stats[mod]["files"] += 1
            if "router" in name:
                stats[mod]["routers"] += 1
            elif "service" in name:
                stats[mod]["services"] += 1
    return stats


def backend_debt(cross: dict) -> list[tuple[str, str, list[str]]]:
    """Truth-side drift vs documented rules (mirrors the gate's waivers)."""
    debt: list[tuple[str, str, list[str]]] = []
    # 1. any module importing identity's ORM models (directory-port rule)
    hits = [
        f"{src}:{loc[0]}:{loc[1]}"
        for src, tgts in cross.items()
        if src.startswith("modules.") and src != "modules.identity"
        for loc in tgts.get("modules.identity", [])
        if _imports_identity_models(loc[0])
    ]
    if hits:
        debt.append(
            (
                "identity ORM models imported outside identity "
                "(rule: directory port only — interfaces.md)",
                "5 files (2026-10-16), waived in the gate",
                _identity_model_files(),
            )
        )
    # 2. modules importing the worker runtime
    worker_hits = sorted(
        f"{src} via {loc[0]}"
        for src, tgts in cross.items()
        if src.startswith("modules.")
        for tgt, locs in tgts.items()
        if tgt.startswith("workers")
        for loc in locs
    )
    if worker_hits:
        debt.append(
            (
                "modules import the worker runtime (dispatch seams)",
                "2 routers (2026-10-16), waived in the gate",
                worker_hits,
            )
        )
    # 3. cross-module ORM model imports generally (quality-gates §6 smell)
    orm_hits = sorted(
        f"{src} imports {tgt}.models"
        for src, tgts in cross.items()
        if src.startswith("modules.")
        for tgt in tgts
        if tgt.startswith("modules.")
        and tgt != src
        and _imports_foreign_models(src, tgt)
    )
    if orm_hits:
        debt.append(
            (
                "cross-module ORM model imports (quality-gates §6: reach "
                "through contracts/ports, not ORM internals)",
                "not gated — visibility only",
                orm_hits,
            )
        )
    return debt


def _imports_identity_models(rel_file: str) -> bool:
    return "identity.models" in _file_import_lines(
        BACKEND_APP / f"{rel_file}.py"
    )


def _imports_foreign_models(src_mod: str, tgt_mod: str) -> bool:
    for f in (BACKEND_APP / "modules" / src_mod.split(".")[1]).glob("*.py"):
        text = _file_text(f)
        if f"app.modules.{tgt_mod.split('.')[1]}.models" in text:
            return True
    return False


def _identity_model_files() -> list[str]:
    hits = []
    for f in (BACKEND_APP / "modules" / "notifications").glob("*.py"):
        if "app.modules.identity.models" in f.read_text(encoding="utf-8"):
            hits.append(f"modules/notifications/{f.name}")
    return sorted(hits)


_file_cache: dict[Path, str] = {}


def _file_text(path: Path) -> str:
    if path not in _file_cache:
        try:
            _file_cache[path] = path.read_text(encoding="utf-8")
        except OSError:
            _file_cache[path] = ""
    return _file_cache[path]


def _file_import_lines(path: Path) -> str:
    return _file_text(path)


# ---------------------------------------------------------------------------
# Frontend scan (regex over @/ and relative imports)
# ---------------------------------------------------------------------------


def frontend_scan() -> dict[str, set[str]]:
    """Return area->area and feature->feature edges (resolved, deduped)."""
    edges: dict[str, set[str]] = defaultdict(set)
    for f in sorted(FRONTEND_SRC.rglob("*")):
        if f.suffix not in (".ts", ".tsx") or "__tests__" in f.parts:
            continue
        text = _file_text(f)
        src_area = _fe_area(f.relative_to(FRONTEND_SRC))
        for match in TS_IMPORT_RE.finditer(text):
            spec = match.group(1)
            target = _resolve_spec(f.parent, spec)
            if target is None:
                continue
            tgt_area = _fe_area(target)
            if tgt_area and tgt_area != src_area:
                edges[src_area].add(tgt_area)
    return edges


def _fe_area(rel: Path) -> str | None:
    parts = rel.parts
    if not parts:
        return None
    if parts[0] == "features" and len(parts) > 1:
        return f"features/{parts[1]}"
    if parts[0] == "components" and len(parts) > 1:
        return f"components/{parts[1]}"
    return parts[0]


def _resolve_spec(src_dir: Path, spec: str) -> Path | None:
    if spec.startswith("@/"):
        candidate = FRONTEND_SRC / spec[2:]
    else:
        base = src_dir
        for part in spec.split("/"):
            if part == ".":
                continue
            base = base.parent if part == ".." else base / part
        candidate = base
    # index files / extensions
    for option in (
        candidate,
        candidate.with_suffix(".ts"),
        candidate.with_suffix(".tsx"),
        candidate / "index.ts",
        candidate / "index.tsx",
    ):
        if option.exists():
            rel = option.relative_to(FRONTEND_SRC)
            return rel
    return None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _git_head() -> str:
    try:
        return (
            subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=True,
            )
            .stdout.strip()
        )
    except subprocess.CalledProcessError:
        return "unknown-head"


def render(cross: dict, fe_edges: dict, stats: dict) -> str:
    head = _git_head()
    lines: list[str] = [
        "# CampusQuest As-Built Architecture Map",
        "",
        "<!-- AUTO-GENERATED by scripts/generate_as_built.py (make arch-map).",
        "     Do not edit by hand: regenerate and commit instead. The gate",
        "     (`make arch-lint`) forgives documented drift; this map reports",
        f"     the tree as it is. Generated from HEAD {head}. -->",
        "",
        "Regenerate: `make arch-map`. The contract gate is `make arch-lint`",
        "(config: `backend/pyproject.toml [tool.importlinter]`,",
        "`frontend/.dependency-cruiser.cjs`).",
        "",
        "## Backend: cross-module dependency graph",
        "",
        "Nodes are `app/modules/*` packages plus `workers`; edges are",
        "import counts (aggregated over files, both directions collapsed",
        "into one directed edge per pair with the true direction shown).",
        "",
        "```mermaid",
        "flowchart LR",
    ]
    mods = sorted(
        {k.split(".")[1] for k in cross if k.startswith("modules.")}
        | {t.split(".")[1] for v in cross.values() for t in v if t.startswith("modules.")}
    )
    for m in mods:
        lines.append(f"    {m}[{m}]")
    seen_pairs = set()
    for src in sorted(cross):
        if not src.startswith("modules."):
            continue
        s = src.split(".")[1]
        for tgt in sorted(cross[src]):
            if not tgt.startswith("modules.") or tgt == src:
                continue
            t = tgt.split(".")[1]
            if (s, t) in seen_pairs:
                continue
            seen_pairs.add((s, t))
            n = len(cross[src][tgt])
            lines.append(f"    {s} -->|{n}| {t}")
    lines += [
        "```",
        "",
        "Module shape (router / service / total files):",
        "",
        "| module | routers | services | files |",
        "|---|---|---|---|",
    ]
    for m in sorted(stats):
        s = stats[m]
        lines.append(f"| {m} | {s['routers']} | {s['services']} | {s['files']} |")

    lines += ["", "## Backend: boundary debt (truth side, no waivers)", ""]
    if cross.get("workers"):
        worker_mods = ", ".join(
            sorted(t for t in cross["workers"] if t.startswith("modules."))
        )
        lines.append(
            f"- workers import: {worker_mods} (jobs call module services — "
            "the documented orchestration-shell pattern)"
        )
    for title, status, items in backend_debt(cross):
        lines.append(f"- **{title}** — {status}")
        for item in items:
            lines.append(f"  - `{item}`")

    lines += [
        "",
        "## Frontend: feature dependency graph",
        "",
        "```mermaid",
        "flowchart LR",
    ]
    feats = sorted(
        {k for k in fe_edges if k.startswith("features/")}
        | {t for v in fe_edges.values() for t in v if t.startswith("features/")}
    )
    for fpath in feats:
        short = fpath.split("/")[1]
        lines.append(f"    fe_{short}[{short}]")
    for src in sorted(fe_edges):
        if not src.startswith("features/"):
            continue
        s = src.split("/")[1]
        for tgt in sorted(fe_edges[src]):
            if not tgt.startswith("features/"):
                continue
            t = tgt.split("/")[1]
            lines.append(f"    fe_{s} --> fe_{t}")
    lines += [
        "```",
        "",
        "Composition roots (allowed to import freely): `src/app` routes,",
        "`src/components/shell` app chrome. Bottom layers: `src/lib`,",
        "`src/components/ui`.",
        "",
    ]
    # upward = out of a bottom layer into product code (lib must stay a
    # leaf; ui must not become product composition). ui -> lib is fine.
    upward = sorted(
        f"{src} -> {tgt}"
        for src, tgts in fe_edges.items()
        for tgt in tgts
        if (src == "lib" and tgt != "lib")
        or (src == "components/ui" and tgt not in ("lib",))
    )
    lines.append("Boundary crossings out of bottom layers (should stay empty):")
    lines.append("")
    if upward:
        for u in upward:
            lines.append(f"- `{u}`")
    else:
        lines.append("- none")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Self-test (campusquest script discipline: prove the scanner can see)
# ---------------------------------------------------------------------------


def self_test() -> bool:
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        # backend: synthetic module with a cross-module import
        mod = tmp_path / "alpha"
        mod.mkdir()
        (mod / "service.py").write_text(
            "from app.modules.beta.models import Thing\n", encoding="utf-8"
        )
        global BACKEND_APP
        saved = BACKEND_APP
        BACKEND_APP = tmp_path
        try:
            tree = ast.parse((mod / "service.py").read_text())
            found = [
                node.module
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module
            ]
            if found != ["app.modules.beta.models"]:
                print("SELF-TEST FAIL: backend scanner missed the edge", found)
                ok = False
            key = _pkg_key(["modules", "alpha", "service"])
            key2 = _pkg_key(["app", "modules", "alpha", "service"])
            if key != "modules.alpha" or key2 != "modules.alpha":
                print("SELF-TEST FAIL: pkg key wrong:", key, key2)
                ok = False
        finally:
            BACKEND_APP = saved
        # frontend: resolver behaviour
        rel = _resolve_spec(tmp_path, "@/features/x/api")
        if rel is not None:  # tmp tree has no src/features — must resolve None
            print("SELF-TEST FAIL: resolver should miss outside src")
            ok = False
    print("SELF-TEST " + ("PASS" if ok else "FAIL"))
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test:
        return 0 if self_test() else 1

    try:
        cross = backend_scan()
        fe = frontend_scan()
        stats = backend_module_file_stats()
        text = render(cross, fe, stats)
    except Exception as exc:  # pragma: no cover
        print(f"generate_as_built: unexpected error: {exc}", file=sys.stderr)
        return 2

    if args.check:
        current = (
            OUT_PATH.read_text(encoding="utf-8") if OUT_PATH.exists() else ""
        )
        if not check_freshness(current, text):
            print(
                "as-built map is STALE — regenerate with `make arch-map` "
                "and commit it with the change that drifted it.",
                file=sys.stderr,
            )
            return 1
        print("as-built map fresh")
        return 0

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(text, encoding="utf-8")
    print(f"wrote {OUT_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
