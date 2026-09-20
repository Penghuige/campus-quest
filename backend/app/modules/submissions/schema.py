# backend/app/modules/submissions/schema.py
"""Submission validation-schema DSL: strict parser for
`tasks.submission_schema` (spec §12).

V1 validates submissions against a small, explainable JSON DSL instead
of arbitrary code. This module is the single authority that turns the
raw JSONB mapping a Task carries into the typed, immutable
`SubmissionSchema` / `ColumnRule` objects the format validators (CSV /
XLSX / SQLite, plan 04 tasks 4-6) consume — a validation run never
re-parses JSON, it dispatches on the parsed `ColumnType` members via
the `COLUMN_TYPES` registry.

Parsing is STRICT by spec: anything the DSL does not define is
REJECTED, not ignored — unknown top-level keys and unknown
column-rule keys raise `SchemaParseError` carrying the offending key's
path, so a typo'd Task config fails loudly at publish time instead of
silently not applying.

Grammar (every top-level key is optional; a missing key means "no
constraint of that kind"):

    allowed_formats:      non-empty subset of {"csv","xlsx","sqlite"}.
                          Absent -> all three. This is the validation-
                          time gate; the upload-time gate stays
                          Task.allowed_file_types (spec §10).
    source_selector:      {"table_name": str} for SQLite and/or
                          {"sheet_name": str} for XLSX; CSV has no
                          selector and ignores the block.
    min_rows / max_rows:  non-negative ints, min <= max, each bounded
                          by MAX_ROWS_CEILING.
    required_columns:     [column, ...] — presence enforced.
    optional_columns:     [column, ...] — type-checked when present.
    allow_extra_columns:  bool, default False (extra columns fail).
    column := {
        "name": str, non-empty, unique across BOTH lists,
        "type": "string" | "integer" | "number" | "boolean" | "datetime",
        "nullable": bool, default False,
        "unique": bool, default False,
        "max_null_ratio": number in [0, 1], optional,
    }

    "required" is NOT a column key — which list the column appears in
    decides it (spec §12 lists requiredness as list membership).

MAX_ROWS_CEILING (10,000,000) bounds Task-authored configs on the same
fail-closed principle as the upload size cap: an abusive or mistaken
schema cannot ask the validation worker for unbounded work. It is an
absolute deployment ceiling — a Task may tighten below it but never
exceed it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from app.modules.submissions.enums import FileType

__all__ = [
    "COLUMN_TYPES",
    "MAX_ROWS_CEILING",
    "ColumnType",
    "ColumnRule",
    "SchemaParseError",
    "SourceSelector",
    "SubmissionSchema",
]

#: Absolute row-bound ceiling for Task-authored schemas (see module
#: docstring): the largest dataset V1 is willing to even count.
MAX_ROWS_CEILING = 10_000_000

# DSL literals -> the module's canonical FileType members, so parsed
# schemas compare directly against the upload-side file-type universe.
_FORMAT_LITERALS: Mapping[str, FileType] = MappingProxyType(
    {"csv": FileType.CSV, "xlsx": FileType.XLSX, "sqlite": FileType.SQLITE}
)

_TOP_LEVEL_KEYS = frozenset(
    {
        "allowed_formats",
        "source_selector",
        "min_rows",
        "max_rows",
        "required_columns",
        "optional_columns",
        "allow_extra_columns",
    }
)
_SELECTOR_KEYS = frozenset({"table_name", "sheet_name"})
_COLUMN_RULE_KEYS = frozenset({"name", "type", "nullable", "unique", "max_null_ratio"})


class SchemaParseError(Exception):
    """A raw schema violates the closed DSL.

    `key` is the dotted path of the offending key
    ("required_columns[1].type", "allowed_formats") or None when the
    schema itself has the wrong shape (not a JSON object). The message
    always names the path so logs read without the attribute.
    """

    def __init__(self, message: str, *, key: str | None = None) -> None:
        super().__init__(message)
        self.key = key


class ColumnType(StrEnum):
    """The closed per-column value-type universe (spec §12)."""

    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    DATETIME = "datetime"


#: Type registry: the frozen literal -> member mapping. `parse`
#: resolves column types through it and the format validators render /
#: dispatch on the members without ever touching raw JSON strings.
COLUMN_TYPES: Mapping[str, ColumnType] = MappingProxyType(
    {member.value: member for member in ColumnType}
)


@dataclass(frozen=True, slots=True)
class SourceSelector:
    """Which table / sheet the validators read.

    `table_name` applies to SQLite inputs, `sheet_name` to XLSX; a
    multi-format schema may carry both and each validator reads only
    its own field. CSV has no selector.
    """

    table_name: str | None = None
    sheet_name: str | None = None


@dataclass(frozen=True, slots=True)
class ColumnRule:
    """One typed column contract.

    `required` comes from list membership (required_columns vs
    optional_columns), never from a rule key; `max_null_ratio` is the
    optional column-level null budget in [0, 1] (moot unless
    `nullable` admits nulls at all).
    """

    name: str
    type: ColumnType
    required: bool
    nullable: bool = False
    unique: bool = False
    max_null_ratio: float | None = None


@dataclass(frozen=True, slots=True)
class SubmissionSchema:
    """The parsed, immutable form of a Task's validation schema."""

    allowed_formats: frozenset[FileType] = field(
        default_factory=lambda: frozenset(FileType)
    )
    source_selector: SourceSelector | None = None
    min_rows: int | None = None
    max_rows: int | None = None
    required_columns: tuple[ColumnRule, ...] = ()
    optional_columns: tuple[ColumnRule, ...] = ()
    allow_extra_columns: bool = False
    # Every declared column by name (required first, then optional, in
    # DSL order) — the single lookup surface for the validators.
    columns: Mapping[str, ColumnRule] = field(
        default_factory=lambda: MappingProxyType({})
    )

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> SubmissionSchema:
        """Strictly parse a raw DSL mapping into typed rules.

        Raises `SchemaParseError` on the FIRST violation, deterministic
        (unknown keys report the alphabetically first offender).
        """
        if not isinstance(raw, Mapping):
            raise SchemaParseError("提交校验 schema 必须是 JSON 对象")

        unknown_top = set(raw.keys()) - _TOP_LEVEL_KEYS
        if unknown_top:
            first = sorted(unknown_top)[0]
            raise SchemaParseError(f"未知的顶层字段: {first}", key=first)

        formats = _parse_allowed_formats(raw)
        selector = _parse_source_selector(raw)
        min_rows = _parse_row_bound(raw, "min_rows")
        max_rows = _parse_row_bound(raw, "max_rows")
        if min_rows is not None and max_rows is not None and min_rows > max_rows:
            raise SchemaParseError(
                f"min_rows ({min_rows}) 不能大于 max_rows ({max_rows})",
                key="min_rows",
            )
        allow_extra = _parse_bool(raw, "allow_extra_columns", False)
        required = _parse_column_list(raw, "required_columns")
        optional = _parse_column_list(raw, "optional_columns")
        _reject_duplicate_columns(required, optional)

        columns: dict[str, ColumnRule] = {}
        for rule in required:
            columns[rule.name] = rule
        for rule in optional:
            columns[rule.name] = rule

        return cls(
            allowed_formats=formats,
            source_selector=selector,
            min_rows=min_rows,
            max_rows=max_rows,
            required_columns=required,
            optional_columns=optional,
            allow_extra_columns=allow_extra,
            columns=MappingProxyType(columns),
        )


def _parse_allowed_formats(raw: Mapping[str, Any]) -> frozenset[FileType]:
    if "allowed_formats" not in raw:
        return frozenset(FileType)
    value = raw["allowed_formats"]
    if not isinstance(value, list):
        raise SchemaParseError("allowed_formats 必须是非空数组", key="allowed_formats")
    if not value:
        raise SchemaParseError("allowed_formats 必须是非空数组", key="allowed_formats")
    formats: set[FileType] = set()
    for item in value:
        member = _FORMAT_LITERALS.get(item) if isinstance(item, str) else None
        if member is None:
            raise SchemaParseError(
                f"allowed_formats 含未知格式: {item!r}，"
                f"允许值为 {sorted(_FORMAT_LITERALS)}",
                key="allowed_formats",
            )
        formats.add(member)
    return frozenset(formats)


def _parse_source_selector(raw: Mapping[str, Any]) -> SourceSelector | None:
    if "source_selector" not in raw:
        return None
    value = raw["source_selector"]
    if not isinstance(value, Mapping):
        raise SchemaParseError(
            "source_selector 必须是 JSON 对象", key="source_selector"
        )
    unknown = set(value.keys()) - _SELECTOR_KEYS
    if unknown:
        first = sorted(unknown)[0]
        raise SchemaParseError(
            f"source_selector 含未知字段: {first}",
            key=f"source_selector.{first}",
        )
    selector = SourceSelector(
        table_name=_selector_name(value, "table_name"),
        sheet_name=_selector_name(value, "sheet_name"),
    )
    if selector.table_name is None and selector.sheet_name is None:
        raise SchemaParseError(
            "source_selector 至少需要 table_name 或 sheet_name 之一",
            key="source_selector",
        )
    return selector


def _selector_name(value: Mapping[str, Any], key: str) -> str | None:
    if key not in value:
        return None
    name = value[key]
    if not isinstance(name, str) or not name:
        raise SchemaParseError(
            f"source_selector.{key} 必须是非空字符串",
            key=f"source_selector.{key}",
        )
    return name


def _parse_row_bound(raw: Mapping[str, Any], key: str) -> int | None:
    if key not in raw:
        return None
    value = raw[key]
    # bool is an int subclass — a literal true/false is a config error,
    # not the number 1/0.
    if isinstance(value, bool) or not isinstance(value, int):
        raise SchemaParseError(f"{key} 必须是非负整数", key=key)
    if value < 0:
        raise SchemaParseError(f"{key} 必须是非负整数", key=key)
    if value > MAX_ROWS_CEILING:
        raise SchemaParseError(
            f"{key} ({value}) 超过绝对上限 {MAX_ROWS_CEILING}", key=key
        )
    return value


def _parse_bool(raw: Mapping[str, Any], key: str, default: bool) -> bool:
    if key not in raw:
        return default
    value = raw[key]
    if not isinstance(value, bool):
        raise SchemaParseError(f"{key} 必须是布尔值", key=key)
    return value


def _parse_column_list(raw: Mapping[str, Any], key: str) -> tuple[ColumnRule, ...]:
    if key not in raw:
        return ()
    value = raw[key]
    if not isinstance(value, list):
        raise SchemaParseError(f"{key} 必须是数组", key=key)
    return tuple(_parse_column(entry, key, index) for index, entry in enumerate(value))


def _parse_column(entry: Any, list_key: str, index: int) -> ColumnRule:
    prefix = f"{list_key}[{index}]"
    if not isinstance(entry, Mapping):
        raise SchemaParseError(f"{prefix} 必须是 JSON 对象", key=prefix)

    unknown = set(entry.keys()) - _COLUMN_RULE_KEYS
    if unknown:
        first = sorted(unknown)[0]
        if first == "required":
            raise SchemaParseError(
                f"{prefix}.required 不是有效字段：列是否必填由其所在的列表决定",
                key=f"{prefix}.required",
            )
        raise SchemaParseError(f"{prefix} 含未知字段: {first}", key=f"{prefix}.{first}")

    name = entry.get("name")
    if not isinstance(name, str) or not name:
        raise SchemaParseError(f"{prefix}.name 必须是非空字符串", key=f"{prefix}.name")

    literal = entry.get("type")
    if not isinstance(literal, str) or literal not in COLUMN_TYPES:
        raise SchemaParseError(
            f"{prefix}.type 必须是 {sorted(COLUMN_TYPES)} 之一",
            key=f"{prefix}.type",
        )

    nullable = _column_bool(entry, prefix, "nullable")
    unique = _column_bool(entry, prefix, "unique")
    ratio = _column_max_null_ratio(entry, prefix)

    return ColumnRule(
        name=name,
        type=COLUMN_TYPES[literal],
        required=list_key == "required_columns",
        nullable=nullable,
        unique=unique,
        max_null_ratio=ratio,
    )


def _column_bool(entry: Mapping[str, Any], prefix: str, key: str) -> bool:
    if key not in entry:
        return False
    value = entry[key]
    if not isinstance(value, bool):
        raise SchemaParseError(f"{prefix}.{key} 必须是布尔值", key=f"{prefix}.{key}")
    return value


def _column_max_null_ratio(entry: Mapping[str, Any], prefix: str) -> float | None:
    if "max_null_ratio" not in entry:
        return None
    value = entry["max_null_ratio"]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SchemaParseError(
            f"{prefix}.max_null_ratio 必须是 [0,1] 内的数字",
            key=f"{prefix}.max_null_ratio",
        )
    # The chained comparison is False for NaN and the infinities, so
    # they fall through to the same rejection.
    if not 0.0 <= value <= 1.0:
        raise SchemaParseError(
            f"{prefix}.max_null_ratio 必须在 [0,1] 内（收到 {value}）",
            key=f"{prefix}.max_null_ratio",
        )
    return float(value)


def _reject_duplicate_columns(
    required: tuple[ColumnRule, ...], optional: tuple[ColumnRule, ...]
) -> None:
    """One name, one rule: duplicates inside a list and overlaps across
    the required/optional lists (the disjointness rule) both fail here,
    on the second occurrence's path.
    """
    seen: dict[str, str] = {}
    for rules, list_key in (
        (required, "required_columns"),
        (optional, "optional_columns"),
    ):
        for index, rule in enumerate(rules):
            first_list = seen.get(rule.name)
            if first_list is not None:
                raise SchemaParseError(
                    f"列名 {rule.name!r} 重复出现（{first_list} 与 {list_key}）",
                    key=f"{list_key}[{index}].name",
                )
            seen[rule.name] = list_key
