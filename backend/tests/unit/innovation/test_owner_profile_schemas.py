import pytest
from pydantic import ValidationError

from app.modules.innovation.owner_schemas import OwnerProfileResponse, OwnerProfileSave

FIELDS = {
    "name": " 小叶 ",
    "student_no": " 001234 ",
    "major": " 计算机 ",
    "grade": " 2026级 ",
}


def test_four_private_fields_are_required_and_trimmed() -> None:
    value = OwnerProfileSave(**FIELDS, version=0)
    assert value.model_dump() == {
        "name": "小叶",
        "student_no": "001234",
        "major": "计算机",
        "grade": "2026级",
        "version": 0,
    }
    for key, limit in {"name": 80, "student_no": 40, "major": 120, "grade": 40}.items():
        for bad in (" \n ", "🌱" * (limit + 1)):
            with pytest.raises(ValidationError):
                OwnerProfileSave(**{**FIELDS, key: bad}, version=0)
        OwnerProfileSave(**{**FIELDS, key: "🌱" * limit}, version=0)
        with pytest.raises(ValidationError):
            OwnerProfileSave(**{k: v for k, v in FIELDS.items() if k != key}, version=0)


@pytest.mark.parametrize(
    "extra", [{"qualified": True}, {"user_id": "x"}, {"role": "ADMIN"}]
)
def test_self_reported_fields_cannot_grant_permissions(extra: dict) -> None:
    with pytest.raises(ValidationError):
        OwnerProfileSave(**FIELDS, version=0, **extra)


@pytest.mark.parametrize("version", [-1, True, "1", 1.5])
def test_expected_version_is_a_strict_nonnegative_integer(version: object) -> None:
    with pytest.raises(ValidationError):
        OwnerProfileSave(**FIELDS, version=version)


def test_response_has_no_account_or_qualification_fields() -> None:
    assert set(OwnerProfileResponse.model_fields) == {
        "name",
        "student_no",
        "major",
        "grade",
        "version",
        "created_at",
        "updated_at",
    }
