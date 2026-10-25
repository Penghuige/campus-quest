import pytest
from pydantic import ValidationError

from app.modules.innovation.operations_schemas import OperationsGrantSave


def test_normalizes_reason() -> None:
    payload = OperationsGrantSave(enabled=True, version=0, reason="  指定运营  ")
    assert payload.reason == "指定运营"


@pytest.mark.parametrize(
    "change",
    [
        {"reason": "  "},
        {"reason": "a" * 501},
        {"version": -1},
        {"version": True},
        {"version": "0"},
        {"enabled": "true"},
        {"role": "ADMIN"},
    ],
)
def test_invalid_input(change) -> None:
    with pytest.raises(ValidationError):
        OperationsGrantSave.model_validate(
            {"enabled": True, "version": 0, "reason": "指定运营", **change}
        )
