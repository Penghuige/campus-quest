"""Regression pins for cross-module forbidden-contract source coverage."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

_PYPROJECT = Path(__file__).resolve().parents[3] / "pyproject.toml"


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
