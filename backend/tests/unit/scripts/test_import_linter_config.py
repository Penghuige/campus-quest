"""Regression pins for cross-module forbidden-contract source coverage."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

_PYPROJECT = Path(__file__).resolve().parents[3] / "pyproject.toml"
_MODULES_DIR = Path(__file__).resolve().parents[3] / "app" / "modules"


def _contract(contracts: list[dict[str, Any]], name_fragment: str) -> dict[str, Any]:
    return next(c for c in contracts if name_fragment in c["name"])


def test_cross_module_forbidden_contracts_cover_nested_business_modules() -> None:
    """``app.modules`` alone does not expand to every child source package.

    The identity rule previously missed a real innovation -> identity.models
    import. Both cross-module forbidden contracts must opt into descendants
    and direct-import checking so that shape cannot silently return.
    """
    contracts = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["tool"][
        "importlinter"
    ]["contracts"]
    for fragment in (
        "Identity ORM models stay behind",
        "Modules must not import the worker runtime",
    ):
        contract = _contract(contracts, fragment)
        assert contract["source_modules"] == ["app.modules.*"]
        assert contract["allow_indirect_imports"] is True


def test_layers_contracts_list_every_service_file() -> None:
    """Every *service*.py in a layered module must be registered in its
    ``routers above services`` contract so new services cannot silently
    bypass layer enforcement (D02-01)."""
    contracts = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["tool"][
        "importlinter"
    ]["contracts"]
    layer_contracts = {
        c["containers"][0]: {
            token.strip() for layer in c["layers"] for token in layer.split(":")
        }
        for c in contracts
        if c.get("type") == "layers"
    }
    for container, declared in layer_contracts.items():
        mod_name = container.removeprefix("app.modules.")
        mod_dir = _MODULES_DIR / mod_name
        actual_services = {p.stem for p in mod_dir.glob("*service*.py")}
        missing = sorted(actual_services - declared)
        assert not missing, f"{container} layers missing services: {missing}"


def test_worker_shell_contract_lists_every_module_router() -> None:
    """Every *router*.py under app/modules/ must be listed in the worker
    forbidden-router contract (D02-02)."""
    contracts = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["tool"][
        "importlinter"
    ]["contracts"]
    worker_contract = _contract(contracts, "Workers are orchestration shells")
    declared = set(worker_contract["forbidden_modules"])
    actual = {
        f"app.modules.{p.parent.name}.{p.stem}"
        for p in _MODULES_DIR.glob("*/*router*.py")
    }
    missing = sorted(actual - declared)
    assert not missing, f"worker forbidden_modules missing routers: {missing}"


def test_innovation_boundary_contract_enforces_zero_task_domain_imports() -> None:
    """innovation-boundary.md claims arch-lint enforces zero imports from
    task-domain modules; pin the corresponding forbidden contract (B07-01)."""
    contracts = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["tool"][
        "importlinter"
    ]["contracts"]
    contract = _contract(contracts, "innovation stays decoupled")
    assert "app.modules.innovation.*" in contract["source_modules"]
    for target in ("tasks", "submissions", "points", "community", "rankings"):
        assert f"app.modules.{target}" in contract["forbidden_modules"]
        assert f"app.modules.{target}.*" in contract["forbidden_modules"]
