"""Upload metadata has exact size bands and display-only safe names."""

import importlib
from uuid import uuid4

import pytest
from pydantic import ValidationError


def schema():
    name = "app.modules.innovation.talent_file_schemas"
    assert importlib.util.find_spec(name) is not None, "talent file schema missing"
    return importlib.import_module(name).TalentFileIntentCreate


@pytest.mark.parametrize("size", [0, -1, 10485761, True, "100"])
def test_file_size_band_and_integer_type(size):
    with pytest.raises(ValidationError):
        schema()(
            request_id=uuid4(),
            name="作品.pdf",
            content_type="application/pdf",
            size=size,
        )


def test_file_maximum_and_path_control_stripping():
    value = schema()(
        request_id=uuid4(),
        name="C:\\private\\作品\r\n.pdf",
        content_type="application/pdf",
        size=10485760,
    )
    assert value.name == "作品.pdf"
    assert value.size == 10485760


@pytest.mark.parametrize("name", ["", " ", "a" * 201, "/", ".."])
def test_invalid_file_names(name):
    with pytest.raises(ValidationError):
        schema()(request_id=uuid4(), name=name, content_type="application/pdf", size=1)


@pytest.mark.parametrize("content_type", ["image/webp", "text/html", "application/zip"])
def test_file_mime_closed_whitelist(content_type):
    with pytest.raises(ValidationError):
        schema()(request_id=uuid4(), name="作品", content_type=content_type, size=1)
