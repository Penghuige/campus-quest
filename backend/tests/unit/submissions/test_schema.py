# backend/tests/unit/submissions/test_schema.py
"""Unit tests for the submission validation-schema DSL parser (spec §12;
backend-engineering §4/§21: pure parsing behavior, no database, no
services).

`SubmissionSchema.parse` turns the raw JSONB mapping stored on
`tasks.submission_schema` into the typed, immutable rule objects the
format validators consume. The DSL is deliberately closed: anything the
spec does not define must be REJECTED, not ignored, so a typo'd Task
config fails loudly at publish time.

Coverage matrix:

- the §12 example (url string unique required, title string required,
  publish_time datetime required, likes integer optional, min_rows 500)
  parses to the exact expected structure, defaults included
- allowed_formats: non-empty subset of {csv, xlsx, sqlite}; absent ->
  all three; empty list / unknown member / non-list / explicit null
  rejected
- source_selector: table_name (SQLite) and/or sheet_name (XLSX);
  empty object / unknown key / non-string / empty string / non-object
  / explicit null rejected; CSV simply ignores the selector
- min_rows / max_rows: non-negative ints (bools are not ints here),
  min <= max, both optional, both bounded by MAX_ROWS_CEILING
  (ceiling itself accepted, ceiling + 1 rejected)
- column rules: the five type literals; name/type required and
  string-y; nullable/unique default False and must be real booleans;
  max_null_ratio bounded to [0, 1] with NaN/inf/bool/string rejected
- unknown keys: top level and column level (including a "required"
  key inside a column — requiredness comes from list membership)
  raise SchemaParseError carrying the offending key's path
- duplicates: the same column name twice in one list, or in both
  lists (the required/optional disjointness rule), rejected
- allow_extra_columns: bool only, default False
- typed surface: SchemaParseError.key pinpoints the offender; rules
  and the parsed schema are frozen dataclasses; COLUMN_TYPES is the
  closed literal registry validators dispatch through
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from app.modules.submissions.enums import FileType
from app.modules.submissions.schema import (
    COLUMN_TYPES,
    MAX_ROWS_CEILING,
    ColumnRule,
    ColumnType,
    SchemaParseError,
    SourceSelector,
    SubmissionSchema,
)


def spec_12_example() -> dict[str, Any]:
    """The spec §12 worked example as raw DSL JSON."""
    return {
        "required_columns": [
            {"name": "url", "type": "string", "unique": True},
            {"name": "title", "type": "string"},
            {"name": "publish_time", "type": "datetime"},
        ],
        "optional_columns": [
            {"name": "likes", "type": "integer"},
        ],
        "min_rows": 500,
    }


def parse_error(raw: Any) -> SchemaParseError:
    """Run parse and return the raised error for field assertions."""
    with pytest.raises(SchemaParseError) as exc_info:
        SubmissionSchema.parse(raw)
    return exc_info.value


def column(
    name: str = "url",
    column_type: str = "string",
    **extra: Any,
) -> dict[str, Any]:
    return {"name": name, "type": column_type, **extra}


# --- happy path -----------------------------------------------------------


def test_spec_12_example_parses_to_expected_structure() -> None:
    schema = SubmissionSchema.parse(spec_12_example())

    assert schema.min_rows == 500
    assert schema.max_rows is None
    assert schema.allow_extra_columns is False
    assert schema.source_selector is None
    # allowed_formats absent -> no validation-time restriction beyond
    # the Task's own upload gate.
    assert schema.allowed_formats == frozenset(FileType)

    (url, title, publish_time) = schema.required_columns
    assert (url.name, title.name, publish_time.name) == (
        "url",
        "title",
        "publish_time",
    )
    assert url.type is ColumnType.STRING
    assert title.type is ColumnType.STRING
    assert publish_time.type is ColumnType.DATETIME
    assert len(schema.optional_columns) == 1
    likes = schema.optional_columns[0]
    assert likes.type is ColumnType.INTEGER

    assert schema.columns["url"].unique is True
    assert schema.columns["url"].required is True
    assert schema.columns["title"].unique is False
    assert schema.columns["publish_time"].nullable is False
    assert schema.columns["likes"].required is False
    assert schema.columns["likes"].max_null_ratio is None
    assert set(schema.columns) == {"url", "title", "publish_time", "likes"}


def test_empty_schema_is_valid() -> None:
    schema = SubmissionSchema.parse({})

    assert schema.allowed_formats == frozenset(FileType)
    assert schema.source_selector is None
    assert schema.min_rows is None
    assert schema.max_rows is None
    assert schema.required_columns == ()
    assert schema.optional_columns == ()
    assert schema.allow_extra_columns is False
    assert dict(schema.columns) == {}


def test_all_five_column_types_accepted() -> None:
    literals = ["string", "integer", "number", "boolean", "datetime"]
    raw = {
        "required_columns": [
            column(name=f"c_{literal}", column_type=literal) for literal in literals
        ],
    }
    schema = SubmissionSchema.parse(raw)

    for literal in literals:
        assert schema.columns[f"c_{literal}"].type is COLUMN_TYPES[literal]


def test_column_rule_defaults() -> None:
    schema = SubmissionSchema.parse(
        {"required_columns": [column()], "optional_columns": []},
    )
    rule = schema.columns["url"]

    assert rule == ColumnRule(
        name="url",
        type=ColumnType.STRING,
        required=True,
        nullable=False,
        unique=False,
        max_null_ratio=None,
    )


def test_column_rule_full_options() -> None:
    schema = SubmissionSchema.parse(
        {
            "optional_columns": [
                column(
                    name="score",
                    column_type="number",
                    nullable=True,
                    unique=False,
                    max_null_ratio=0.25,
                ),
            ],
        },
    )
    rule = schema.columns["score"]

    assert rule.required is False
    assert rule.nullable is True
    assert rule.unique is False
    assert rule.max_null_ratio == 0.25


def test_parsed_objects_are_frozen() -> None:
    schema = SubmissionSchema.parse(spec_12_example())

    with pytest.raises(dataclasses.FrozenInstanceError):
        schema.min_rows = 1  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        schema.columns["url"].unique = False  # type: ignore[misc]


# --- allowed_formats ------------------------------------------------------


@pytest.mark.parametrize(
    ("raw_formats", "expected"),
    [
        (["csv"], {FileType.CSV}),
        (["csv", "xlsx"], {FileType.CSV, FileType.XLSX}),
        (["sqlite"], {FileType.SQLITE}),
        (["xlsx", "csv", "sqlite"], set(FileType)),
    ],
)
def test_allowed_formats_valid_subsets(
    raw_formats: list[str], expected: set[FileType]
) -> None:
    schema = SubmissionSchema.parse({"allowed_formats": raw_formats})

    assert schema.allowed_formats == frozenset(expected)


@pytest.mark.parametrize(
    "bad_formats",
    [
        [],
        ["csv", "parquet"],
        ["CSV"],
        ["csv", 1],
        "csv",
        {"csv": True},
        42,
        None,
        [None],
        [True],
    ],
)
def test_allowed_formats_invalid(bad_formats: Any) -> None:
    error = parse_error({"allowed_formats": bad_formats})

    assert error.key == "allowed_formats"


# --- source_selector ------------------------------------------------------


def test_source_selector_table_name() -> None:
    schema = SubmissionSchema.parse(
        {"source_selector": {"table_name": "posts"}},
    )

    assert schema.source_selector == SourceSelector(table_name="posts")


def test_source_selector_sheet_name() -> None:
    schema = SubmissionSchema.parse(
        {"source_selector": {"sheet_name": "Sheet1"}},
    )

    assert schema.source_selector == SourceSelector(sheet_name="Sheet1")


def test_source_selector_both_names() -> None:
    # A multi-format schema may name both targets; each validator reads
    # the field that applies to its format (CSV ignores the selector).
    schema = SubmissionSchema.parse(
        {"source_selector": {"table_name": "posts", "sheet_name": "数据"}},
    )

    assert schema.source_selector == SourceSelector(
        table_name="posts", sheet_name="数据"
    )


@pytest.mark.parametrize(
    "bad_selector",
    [
        {},
        {"worksheet": "Sheet1"},
        {"table_name": 123},
        {"table_name": ""},
        {"table_name": None},
        {"sheet_name": ""},
        "posts",
        None,
        ["posts"],
    ],
)
def test_source_selector_invalid(bad_selector: Any) -> None:
    error = parse_error({"source_selector": bad_selector})

    assert error.key is not None
    assert error.key.startswith("source_selector")


# --- min_rows / max_rows --------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        {"min_rows": 0},
        {"max_rows": 0},
        {"min_rows": 0, "max_rows": 0},
        {"min_rows": 500},
        {"max_rows": 500},
        {"min_rows": 500, "max_rows": 500},
        {"min_rows": 1, "max_rows": MAX_ROWS_CEILING},
        {"max_rows": MAX_ROWS_CEILING},
        {"min_rows": MAX_ROWS_CEILING, "max_rows": MAX_ROWS_CEILING},
    ],
)
def test_row_bounds_valid(raw: dict[str, Any]) -> None:
    schema = SubmissionSchema.parse(raw)

    assert schema.min_rows == raw.get("min_rows")
    assert schema.max_rows == raw.get("max_rows")


@pytest.mark.parametrize("key", ["min_rows", "max_rows"])
def test_row_bounds_invalid_values(key: str) -> None:
    for bad in (-1, 1.5, "500", True, None, [500]):
        error = parse_error({key: bad})

        assert error.key == key


@pytest.mark.parametrize(
    "key",
    ["min_rows", "max_rows"],
)
def test_row_bounds_ceiling_rejects_overflow(key: str) -> None:
    error = parse_error({key: MAX_ROWS_CEILING + 1})

    assert error.key == key
    assert str(MAX_ROWS_CEILING) in str(error)


def test_min_rows_above_max_rows_rejected() -> None:
    error = parse_error({"min_rows": 501, "max_rows": 500})

    assert error.key == "min_rows"


# --- required_columns / optional_columns ----------------------------------


def test_empty_required_list_allowed() -> None:
    schema = SubmissionSchema.parse(
        {
            "required_columns": [],
            "optional_columns": [column(name="likes", column_type="integer")],
        },
    )

    assert schema.required_columns == ()
    assert [rule.name for rule in schema.optional_columns] == ["likes"]
    assert schema.columns["likes"].required is False


def test_column_lists_must_be_arrays() -> None:
    for key in ("required_columns", "optional_columns"):
        for bad in {"name": "url"}, "url", 42, None:
            error = parse_error({key: bad})

            assert error.key == key


def test_column_entries_must_be_objects() -> None:
    error = parse_error({"required_columns": ["url"]})

    assert error.key == "required_columns[0]"


@pytest.mark.parametrize(
    ("entry", "bad_key"),
    [
        ({"type": "string"}, "name"),
        ({"name": "url"}, "type"),
        ({"name": "", "type": "string"}, "name"),
        ({"name": 123, "type": "string"}, "name"),
        ({"name": "url", "type": "text"}, "type"),
        ({"name": "url", "type": 1}, "type"),
        ({"name": "url", "type": None}, "type"),
        ({"name": "url", "type": ["string"]}, "type"),
        ({"name": "url", "type": "string", "nullable": "yes"}, "nullable"),
        ({"name": "url", "type": "string", "nullable": 1}, "nullable"),
        ({"name": "url", "type": "string", "nullable": None}, "nullable"),
        ({"name": "url", "type": "string", "unique": "no"}, "unique"),
        ({"name": "url", "type": "string", "unique": 0}, "unique"),
        ({"name": "url", "type": "string", "max_null_ratio": "0.5"}, "max_null_ratio"),
        ({"name": "url", "type": "string", "max_null_ratio": True}, "max_null_ratio"),
        ({"name": "url", "type": "string", "max_null_ratio": None}, "max_null_ratio"),
    ],
)
def test_column_rule_field_validation(entry: dict[str, Any], bad_key: str) -> None:
    error = parse_error({"required_columns": [entry]})

    assert error.key == f"required_columns[0].{bad_key}"


@pytest.mark.parametrize("ratio", [0, 1, 0.0, 1.0, 0.5, 0.25])
def test_max_null_ratio_bounds_valid(ratio: float) -> None:
    schema = SubmissionSchema.parse(
        {"optional_columns": [column(name="likes", max_null_ratio=ratio)]},
    )

    assert schema.columns["likes"].max_null_ratio == ratio


@pytest.mark.parametrize("ratio", [-0.01, 1.01, -1, 2, float("nan"), float("inf")])
def test_max_null_ratio_bounds_invalid(ratio: float) -> None:
    error = parse_error(
        {"optional_columns": [column(name="likes", max_null_ratio=ratio)]},
    )

    assert error.key == "optional_columns[0].max_null_ratio"


def test_duplicate_column_within_one_list_rejected() -> None:
    error = parse_error(
        {
            "required_columns": [
                column(name="url"),
                column(name="title"),
                column(name="url"),
            ],
        },
    )

    assert error.key == "required_columns[2].name"


def test_column_in_both_lists_rejected() -> None:
    error = parse_error(
        {
            "required_columns": [column(name="url")],
            "optional_columns": [column(name="likes"), column(name="url")],
        },
    )

    assert error.key == "optional_columns[1].name"


def test_optional_list_internal_duplicate_also_rejected() -> None:
    error = parse_error(
        {
            "optional_columns": [column(name="likes"), column(name="likes")],
        },
    )

    assert error.key == "optional_columns[1].name"


# --- unknown keys ---------------------------------------------------------


def test_unknown_top_level_key_rejected() -> None:
    error = parse_error({**spec_12_example(), "requred_columns": []})

    assert error.key == "requred_columns"


def test_unknown_top_level_keys_reported_deterministically() -> None:
    error = parse_error({"zz": 1, "aa": 2})

    assert error.key == "aa"


def test_unknown_column_rule_key_rejected() -> None:
    error = parse_error(
        {
            "required_columns": [
                column(name="url", display_name="链接"),
            ],
        },
    )

    assert error.key == "required_columns[0].display_name"


def test_required_key_inside_column_rule_rejected() -> None:
    # Requiredness is decided by which list the column appears in; a
    # "required" key inside the rule is not part of the DSL.
    error = parse_error(
        {"required_columns": [column(name="url", required=False)]},
    )

    assert error.key == "required_columns[0].required"


# --- allow_extra_columns --------------------------------------------------


@pytest.mark.parametrize("value", [True, False])
def test_allow_extra_columns_boolean(value: bool) -> None:
    schema = SubmissionSchema.parse({"allow_extra_columns": value})

    assert schema.allow_extra_columns is value


@pytest.mark.parametrize("bad", ["true", 1, 0, None, []])
def test_allow_extra_columns_invalid(bad: Any) -> None:
    error = parse_error({"allow_extra_columns": bad})

    assert error.key == "allow_extra_columns"


# --- parser surface -------------------------------------------------------


@pytest.mark.parametrize("not_a_mapping", ["{}", [1], 42, None, True])
def test_schema_must_be_a_mapping(not_a_mapping: Any) -> None:
    error = parse_error(not_a_mapping)

    assert error.key is None


def test_error_is_typed_with_offending_key() -> None:
    error = parse_error({"max_rows": -1})

    assert isinstance(error, Exception)
    assert error.key == "max_rows"
    assert "max_rows" in str(error)


def test_column_type_registry_is_the_closed_literal_set() -> None:
    assert set(COLUMN_TYPES) == {
        "string",
        "integer",
        "number",
        "boolean",
        "datetime",
    }
    assert COLUMN_TYPES["datetime"] is ColumnType.DATETIME
    assert all(literal == member.value for literal, member in COLUMN_TYPES.items())
