# backend/tests/unit/notifications/test_templates.py
"""Unit tests for constrained notification template rendering (spec
§25.5, §33; plan 07 T2).

Spec §25.5 pins the safety contract: "模板渲染必须使用受限变量，不执行代码".
These tests prove the renderer honors it by construction, not by policy:

- every ``{...}`` group in a template must be exactly one variable name
  from the event type's frozen whitelist. Anything else — an unknown
  name, attribute access, positional indexes, filters, conversions,
  format specs, Jinja blocks — raises `InvalidTemplateError`, an
  admin-facing render error naming the offending placeholder. An Admin
  editing a template (Plan 08) therefore cannot smuggle an expression
  into a message.
- variable VALUES are never re-scanned: a value that itself contains
  ``{...}`` or backslash escapes renders literally, byte-for-byte. A
  student-controlled string (task title, review comment) cannot inject
  a second substitution pass, and re.sub replacement escapes
  (``\\g<0>``, ``\\1``) cannot rewrite the output.
- a whitelisted variable without a value raises the distinct typed
  error `MissingTemplateVariableError` — a caller/dispatch bug, not a
  template problem, so the two failure classes never masquerade as
  each other.
- the seed templates (8 event types x 3 channels, spec §25/§25.1
  product language: concise Chinese) are complete and internally valid:
  every seed renders with a full variable set, so whitelist/seed drift
  fails loudly here instead of at first dispatch.

The probes below are exactly the inputs that execute or mangle under
str.format / Jinja: ``{0.__class__}`` walks an object, ``{{ 7*7 }}``
evaluates to 49, ``{x!r}`` converts, and ``\\g<0>`` backreferences —
here every one of them is rejected, never evaluated.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping

import pytest

from app.modules.notifications.enums import NotificationChannel, NotificationEventType
from app.modules.notifications.templates import (
    DEFAULT_TEMPLATES,
    EVENT_VARIABLES,
    InvalidTemplateError,
    MissingTemplateVariableError,
    RenderedMessage,
    TemplateText,
    UnsafeTemplateMarkupError,
    find_unsafe_template_marker,
    render_template,
    validate_admin_template,
)

_IN_APP = NotificationChannel.IN_APP
_EMAIL = NotificationChannel.EMAIL
_SMS = NotificationChannel.SMS


def _full_variables(event_type: NotificationEventType) -> dict[str, str]:
    """Every whitelisted variable for the event, each marked by brackets
    so a substituted output is distinguishable from surviving template
    text."""

    return {name: f"[{name}]" for name in sorted(EVENT_VARIABLES[event_type])}


def _pair_id(pair: tuple[NotificationEventType, NotificationChannel]) -> str:
    return f"{pair[0].value}-{pair[1].value}"


def _all_pairs() -> Iterator[tuple[NotificationEventType, NotificationChannel]]:
    for event_type in NotificationEventType:
        for channel in NotificationChannel:
            yield event_type, channel


# --- seed completeness and renderability --------------------------------------


def test_seed_templates_cover_every_event_type_and_channel() -> None:
    """8 event types x 3 channels = 24 seeds; nothing missing, nothing
    extra (spec §25 event list, §25.1 channel set)."""

    assert set(DEFAULT_TEMPLATES) == set(_all_pairs())


def test_every_event_type_has_a_variable_whitelist() -> None:
    assert set(EVENT_VARIABLES) == set(NotificationEventType)


_SORTED_PAIRS = sorted(
    (
        (event_type, channel)
        for event_type in NotificationEventType
        for channel in NotificationChannel
    ),
    key=_pair_id,
)


@pytest.mark.parametrize(
    ("event_type", "channel"),
    _SORTED_PAIRS,
    ids=[_pair_id(pair) for pair in _SORTED_PAIRS],
)
def test_every_seed_template_renders_with_a_full_variable_set(
    event_type: NotificationEventType, channel: NotificationChannel
) -> None:
    message = render_template(event_type, channel, _full_variables(event_type))

    assert isinstance(message, RenderedMessage)
    seed = DEFAULT_TEMPLATES[(event_type, channel)]
    # Every placeholder actually used by the seed was substituted...
    for name in EVENT_VARIABLES[event_type]:
        placeholder = "{" + name + "}"
        if placeholder in seed.title:
            assert f"[{name}]" in message.title
        if placeholder in seed.body:
            assert f"[{name}]" in message.body
    # ...and no placeholder survived unsubstituted into the snapshot.
    for name in EVENT_VARIABLES[event_type]:
        assert "{" + name + "}" not in message.title
        assert "{" + name + "}" not in message.body


def test_deadline_4h_sms_seed_substitutes_variables() -> None:
    message = render_template(
        NotificationEventType.ASSIGNMENT_DEADLINE_4H,
        _SMS,
        {"task_title": "问卷数据清洗", "deadline_at": "10-01 12:00"},
    )

    assert "问卷数据清洗" in message.body
    assert "10-01 12:00" in message.body
    assert "{task_title}" not in message.body
    assert message.title  # non-empty title for the inbox list (spec §28)


def test_custom_template_replaces_the_seed_for_that_pair() -> None:
    """The Plan 08 seam: a NotificationTemplate row's title/template_body
    are passed as TemplateText and rendered through the exact same
    constrained scanner as the seeds."""

    override = TemplateText(title="提醒：{task_title}", body="自定义正文 {deadline_at}")
    message = render_template(
        NotificationEventType.ASSIGNMENT_DEADLINE_24H,
        _IN_APP,
        {"task_title": "T", "deadline_at": "D"},
        template=override,
    )

    assert message == RenderedMessage(title="提醒：T", body="自定义正文 D")


# --- template safety: unknown names, expressions, re-scan ----------------------


def test_unknown_variable_name_is_an_invalid_template_error() -> None:
    """`nickname` is a real User column but appears in no event's
    whitelist — referencing it means the admin template is invalid, and
    the error names the culprit."""

    override = TemplateText(title="你好，{nickname}", body="正文")
    with pytest.raises(InvalidTemplateError) as excinfo:
        render_template(
            NotificationEventType.REVISION_REQUIRED,
            _IN_APP,
            _full_variables(NotificationEventType.REVISION_REQUIRED),
            template=override,
        )

    assert "nickname" in str(excinfo.value)


CODE_EXECUTION_PROBES = [
    "{__class__}",
    "{self.__class__.__init__}",
    "{task_title.__class__}",
    "{task_title.__class__.__mro__}",
    "{0}",
    "{0.__init__}",
    "{task_title[0]}",
    "{task_title|safe}",
    "{ config.items }",
    "{os.system('touch /tmp/cq-pwned')}",
    "{task_title!r}",
    "{task_title:>20}",
    "{{ 7*7 }}",
    "{% for row in rows %}",
]


@pytest.mark.parametrize("probe", CODE_EXECUTION_PROBES)
def test_expression_probes_are_rejected_never_evaluated(probe: str) -> None:
    """Attribute walks, indexes, filters, conversions, format specs, and
    Jinja blocks all fail the variable-name grammar or the whitelist and
    raise InvalidTemplateError. None is ever evaluated (no 49, no
    /tmp/cq-pwned, no repr)."""

    event_type = NotificationEventType.SUBMISSION_APPROVED
    override = TemplateText(title=probe, body=probe)
    with pytest.raises(InvalidTemplateError):
        render_template(
            event_type, _EMAIL, _full_variables(event_type), template=override
        )


def test_variable_values_are_never_rescanned() -> None:
    """A hostile VALUE (student-controlled task title) containing
    placeholders, Jinja markers, or re.sub replacement escapes renders
    literally — single-pass substitution, no injection."""

    hostile = "{{ 7*7 }} {nickname} \\g<0> \\1 {task_title}"
    message = render_template(
        NotificationEventType.SUBMISSION_APPROVED,
        _IN_APP,
        {"task_title": hostile, "reward_points": "10"},
    )

    assert hostile in message.body


def test_unbalanced_brace_is_plain_literal_text() -> None:
    """A `{` with no matching `}` is not a placeholder; it renders as
    itself rather than erroring on ordinary prose."""

    override = TemplateText(title="t", body="《{task_title》 缺右括号")
    message = render_template(
        NotificationEventType.ACCOUNT_SECURITY,
        _IN_APP,
        {"event_summary": "s", "event_time": "t"},
        template=override,
    )

    assert "《{task_title》" in message.body


# --- typed caller errors -------------------------------------------------------


@pytest.mark.parametrize("variables", [{}, {"task_title": "T"}])
def test_missing_whitelisted_variable_is_a_typed_error(
    variables: Mapping[str, str],
) -> None:
    """`deadline_at` is whitelisted for the deadline events, so the
    template is valid — but no value was supplied at render time. That
    is a distinct, non-admin-facing failure (the caller lost a
    variable), and must not be classifiable as InvalidTemplateError."""

    override = TemplateText(title="截止提醒", body="截止于 {deadline_at}")
    with pytest.raises(MissingTemplateVariableError) as excinfo:
        render_template(
            NotificationEventType.ASSIGNMENT_DEADLINE_24H,
            _SMS,
            variables,
            template=override,
        )

    assert "deadline_at" in str(excinfo.value)
    assert not isinstance(excinfo.value, InvalidTemplateError)


def test_none_valued_variable_counts_as_missing() -> None:
    override = TemplateText(title="{item_name}", body="b")
    with pytest.raises(MissingTemplateVariableError):
        render_template(
            NotificationEventType.REWARD_REDEMPTION_APPROVED,
            _EMAIL,
            {"item_name": None, "points_spent": "10"},
            template=override,
        )


def test_non_string_variable_value_is_rejected() -> None:
    """The renderer never formats values itself (dates/numbers are the
    caller's job), so a non-str value is a TypeError at the boundary."""

    with pytest.raises(TypeError):
        render_template(
            NotificationEventType.SUBMISSION_APPROVED,
            _IN_APP,
            {"task_title": 42, "reward_points": "10"},
        )


def test_unknown_event_type_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="event type"):
        render_template("NOT_AN_EVENT", _IN_APP, {})


def test_unknown_channel_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="channel"):
        render_template(NotificationEventType.SUBMISSION_APPROVED, "PUSH", {})


# --- the write-time gate for Admin edits (Plan 08 T5) --------------------------------


@pytest.mark.parametrize(
    "text",
    ["{{task_title}}", "{% if x %}y{% endif %}", "${inject}", "a}}b", "{{ 7*7 }}"],
)
def test_marker_scan_finds_every_unsafe_sequence(text: str) -> None:
    assert find_unsafe_template_marker(text) is not None


def test_marker_scan_passes_pure_placeholder_text() -> None:
    assert find_unsafe_template_marker("《{task_title}》将于{deadline_at}截止") is None
    assert find_unsafe_template_marker("} 与 { 不成对即为字面文本") is None


def test_jinja_expression_would_render_literally_so_write_time_rejects_it() -> None:
    """Why the marker list exists: ``{{task_title}}`` passes the render
    grammar (the INNER ``{task_title}`` matches) and would ship literal
    braces — the write-time gate refuses the edit before it persists."""
    with pytest.raises(UnsafeTemplateMarkupError) as excinfo:
        validate_admin_template(
            NotificationEventType.ASSIGNMENT_DEADLINE_4H,
            _SMS,
            title="t",
            body="{{task_title}}",
        )
    assert excinfo.value.field == "template_body"
    assert excinfo.value.marker == "{{"


def test_write_time_gate_applies_the_render_grammar_to_both_fields() -> None:
    event = NotificationEventType.SUBMISSION_APPROVED
    # Grammar failure in the title (uppercase); whitelist failure in
    # the body ({item_name} belongs to a DIFFERENT event — the
    # whitelist is per-event).
    with pytest.raises(InvalidTemplateError):
        validate_admin_template(event, _IN_APP, title="{Task_Title}", body="b")
    with pytest.raises(InvalidTemplateError) as excinfo:
        validate_admin_template(
            event, _IN_APP, title="t", body="{item_name} 不是本事件的变量"
        )
    assert excinfo.value.placeholder == "item_name"


def test_write_time_gate_accepts_exactly_what_render_honors() -> None:
    for (event_type, channel), seed in DEFAULT_TEMPLATES.items():
        # Every seed is a legal Admin edit: no markers, placeholders
        # all whitelisted.
        validate_admin_template(event_type, channel, title=seed.title, body=seed.body)


def test_write_time_gate_keeps_the_programming_error_boundary() -> None:
    with pytest.raises(ValueError, match="event type"):
        validate_admin_template("NOT_AN_EVENT", _IN_APP, title="t", body="b")
    with pytest.raises(ValueError, match="channel"):
        validate_admin_template(
            NotificationEventType.SUBMISSION_APPROVED, "PUSH", title="t", body="b"
        )
