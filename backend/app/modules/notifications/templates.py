# backend/app/modules/notifications/templates.py
"""Constrained notification template rendering (spec §25.5, §33).

Spec §25.5: "模板渲染必须使用受限变量，不执行代码。" This module implements
that sentence as a manual single-pass scanner — deliberately NOT Jinja,
`eval`, or `str.format`:

- Template text is admin-influenced (spec §25.5: Admin edits
  NotificationTemplate rows; Plan 08 adds the editing surface). A
  placeholder is exactly ``{name}`` where ``name`` matches
  ``[a-z][a-z0-9_]*`` AND belongs to the rendering event type's frozen
  whitelist (`EVENT_VARIABLES`). Attribute walks (``x.__class__``),
  positional indexes (``{0}``), filters (``|safe``), conversions
  (``!r``), format specs (``:>20``), and Jinja blocks all fail the
  grammar or the whitelist and raise `InvalidTemplateError` — an
  admin-facing render error naming the offending placeholder. Nothing
  is ever parsed as an expression, so there is nothing to sanitize.
  Plan 08's write-time gate (`validate_admin_template`,
  `UnsafeTemplateMarkupError`) applies this same grammar — plus the
  ``{{``/``}}``/``{%``/``${`` marker rejection — BEFORE a row can
  persist, so a bad edit fails at write time instead of at the next
  dispatch.
- Substitution is one `re.sub` pass with a FUNCTION replacement: the
  function's return string is inserted verbatim (no backreference
  processing, no re-scan). A variable VALUE containing ``{...}`` or
  ``\\g<0>`` therefore renders literally — user-influenced strings
  (task titles, review comments) cannot inject a second substitution
  pass. The str.format-style index tricks are additionally impossible
  because names are dict-keyed, not positional.
- Two failure classes stay distinguishable:
  `InvalidTemplateError` — the template text itself references a
  non-whitelisted placeholder (an Admin problem; Plan 08 surfaces it
  in template editing) — versus `MissingTemplateVariableError` — a
  whitelisted variable with no value at render time (a caller/dispatch
  bug). Both are module-local `ValueError`s on purpose: they are
  internal service failures that the dispatch layer turns into SKIPPED
  or FAILED deliveries, not §29 API envelope codes (the registry is
  frozen by interfaces.md and carries no notification-template code).
- Values must already be `str`: the renderer never formats dates or
  numbers itself (backend-engineering §11 keeps timezone/locale
  decisions with the caller), so a non-str value is a `TypeError` at
  this boundary.

Seed templates (`DEFAULT_TEMPLATES`) cover all 8 event types x 3
channels with concise Chinese product copy (spec §25/§25.1; SMS bodies
stay short for single-segment delivery). They are the FALLBACK: the
Plan 08 consumption wiring (PR #5 gfix C) has the registration port
and the delivery service consult enabled `NotificationTemplate` rows
first — `render_template`'s `template=` parameter is the seam, and a
row's `title`/`template_body` flow through this exact same scanner
(no row, or a disabled one for the snapshot, means the seed renders).
`render_snapshot_template` is the dispatch-side sibling: a managed
SMS/EMAIL row renders from the notification snapshot
(``{"title", "body"}`` — the ports' frozen variable contract) because
the T1 schema persists no event payload, so the per-event render
variables exist only at record time. An import-time self-check
re-validates every seed against the whitelists so drift between
`EVENT_VARIABLES` and `DEFAULT_TEMPLATES` fails at import, not at
first render.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from app.modules.notifications.enums import NotificationChannel, NotificationEventType

# A placeholder is a braced group with no nested braces. Content is
# matched lazily and validated against the name grammar below — the
# regex alone grants nothing.
_PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")

# Variable-name grammar: lowercase snake_case, no leading underscore.
# This grammar (not a sanitizer) is what makes attribute access and
# dunder walks structurally unrepresentable.
_VARIABLE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

_ALL_CHANNELS = frozenset(NotificationChannel)


class TemplateRenderError(ValueError):
    """Base for the two render-time failures below (module-local on
    purpose — see module docstring)."""


class InvalidTemplateError(TemplateRenderError):
    """Template text contains a braced group that is not exactly one
    whitelisted variable name for the event type. Admin-facing: the
    template row (or seed) is wrong, the variables were fine."""

    def __init__(
        self,
        event_type: NotificationEventType | str,
        channel: NotificationChannel | str,
        placeholder: str,
        known: frozenset[str],
    ) -> None:
        self.event_type = event_type
        self.channel = channel
        self.placeholder = placeholder
        self.known = known
        sorted_known = ", ".join(sorted(known)) or "(none)"
        super().__init__(
            f"invalid notification template for {event_type}/{channel}: "
            f"placeholder {placeholder!r} is not a whitelisted variable "
            f"(known variables: {sorted_known})"
        )


class MissingTemplateVariableError(TemplateRenderError):
    """The template is valid but the render call supplied no value for a
    whitelisted variable it references. A caller/dispatch bug, not an
    Admin problem."""

    def __init__(
        self,
        event_type: NotificationEventType | str,
        channel: NotificationChannel | str,
        variable: str,
    ) -> None:
        self.event_type = event_type
        self.channel = channel
        self.variable = variable
        super().__init__(
            f"missing value for notification template variable "
            f"{variable!r} ({event_type}/{channel})"
        )


class UnsafeTemplateMarkupError(TemplateRenderError):
    """Admin-edit rejection (Plan 08 T5): the text carries a
    Jinja/injection-style marker (``{{``, ``}}``, ``{%``, ``${``) that
    pure ``{name}`` substitution would render LITERALLY — the admin
    would ship braces instead of the expression they meant, and any
    downstream provider that interprets such markup would gain code the
    renderer never sanctioned. Module-local like its siblings: the
    admin surface converts it to a §29 422, the registry carries no
    notification-template code."""

    def __init__(
        self,
        event_type: NotificationEventType | str,
        channel: NotificationChannel | str,
        field: str,
        marker: str,
    ) -> None:
        self.event_type = event_type
        self.channel = channel
        self.field = field
        self.marker = marker
        super().__init__(
            f"unsafe markup in notification template {event_type}/{channel}: "
            f"{field} contains {marker!r} (templates are pure "
            "{name} placeholder substitution)"
        )


@dataclass(frozen=True)
class TemplateText:
    """A template pair as stored on NotificationTemplate (title +
    template_body). The managed-row seam: registration and dispatch
    build one from a row and pass it to the render functions."""

    title: str
    body: str


@dataclass(frozen=True)
class RenderedMessage:
    """The rendered snapshot persisted onto `Notification.title/body`
    (models.py: a later Admin template edit never rewrites an
    already-created notification)."""

    title: str
    body: str


# --- per-event variable whitelists (spec §25.5: 受限变量) -----------------------
#
# Frozen per event type and shared across channels: the variables an
# event may carry are a property of the DOMAIN event, not of the pipe
# it rides. Every name is referenced by at least one seed below (the
# import-time self-check would otherwise be the only witness).

EVENT_VARIABLES: dict[NotificationEventType, frozenset[str]] = {
    NotificationEventType.ASSIGNMENT_DEADLINE_24H: frozenset(
        {"task_title", "deadline_at"}
    ),
    NotificationEventType.ASSIGNMENT_DEADLINE_4H: frozenset(
        {"task_title", "deadline_at"}
    ),
    NotificationEventType.REVISION_REQUIRED: frozenset(
        {"task_title", "revision_deadline_at", "review_comment"}
    ),
    NotificationEventType.SUBMISSION_APPROVED: frozenset(
        {"task_title", "reward_points"}
    ),
    NotificationEventType.SUBMISSION_VALIDATION_FAILED: frozenset(
        {"task_title", "validation_summary"}
    ),
    NotificationEventType.REWARD_REDEMPTION_APPROVED: frozenset(
        {"item_name", "points_spent"}
    ),
    NotificationEventType.REWARD_REDEMPTION_REJECTED: frozenset(
        {"item_name", "rejection_reason", "points_refunded"}
    ),
    NotificationEventType.ACCOUNT_SECURITY: frozenset({"event_summary", "event_time"}),
}


# --- seed templates (8 event types x 3 channels; Plan 08 adds DB overrides) ----
#
# Concise Chinese per the product language; SMS bodies short enough for
# a single segment; EMAIL bodies slightly fuller with a sign-off. The
# checker invariants: seeds use only whitelisted placeholders, and
# deadline SMS/EMAIL bodies carry the deadline variables the §25.2
# reminder is about.

_SMS_SIGNATURE = "【CampusQuest】"
_EMAIL_SIGNOFF = "\n—— CampusQuest"

DEFAULT_TEMPLATES: dict[
    tuple[NotificationEventType, NotificationChannel], TemplateText
] = {
    (
        NotificationEventType.ASSIGNMENT_DEADLINE_24H,
        NotificationChannel.SMS,
    ): TemplateText(
        title="任务24小时后截止",
        body="您领取的任务《{task_title}》将于{deadline_at}截止，请尽快提交。"
        + _SMS_SIGNATURE,
    ),
    (
        NotificationEventType.ASSIGNMENT_DEADLINE_24H,
        NotificationChannel.EMAIL,
    ): TemplateText(
        title="任务将于24小时后截止",
        body=(
            "您好：\n"
            "您领取的任务《{task_title}》将于{deadline_at}截止，请及时完成并提交。\n"
            "如已提交，请忽略本邮件。" + _EMAIL_SIGNOFF
        ),
    ),
    (
        NotificationEventType.ASSIGNMENT_DEADLINE_24H,
        NotificationChannel.IN_APP,
    ): TemplateText(
        title="任务将于24小时后截止",
        body="您领取的任务《{task_title}》将于{deadline_at}截止，请尽快提交。",
    ),
    (
        NotificationEventType.ASSIGNMENT_DEADLINE_4H,
        NotificationChannel.SMS,
    ): TemplateText(
        title="任务4小时后截止",
        body="您领取的任务《{task_title}》将于{deadline_at}截止，请立即提交。"
        + _SMS_SIGNATURE,
    ),
    (
        NotificationEventType.ASSIGNMENT_DEADLINE_4H,
        NotificationChannel.EMAIL,
    ): TemplateText(
        title="任务将于4小时后截止",
        body=(
            "您好：\n"
            "您领取的任务《{task_title}》将于{deadline_at}截止，请尽快完成并提交。\n"
            "如已提交，请忽略本邮件。" + _EMAIL_SIGNOFF
        ),
    ),
    (
        NotificationEventType.ASSIGNMENT_DEADLINE_4H,
        NotificationChannel.IN_APP,
    ): TemplateText(
        title="任务将于4小时后截止",
        body="您领取的任务《{task_title}》将于{deadline_at}截止，请立即提交。",
    ),
    (
        NotificationEventType.REVISION_REQUIRED,
        NotificationChannel.SMS,
    ): TemplateText(
        title="提交需修改",
        body="您在任务《{task_title}》的提交需修改，请在{revision_deadline_at}前重新提交。"
        + _SMS_SIGNATURE,
    ),
    (
        NotificationEventType.REVISION_REQUIRED,
        NotificationChannel.EMAIL,
    ): TemplateText(
        title="提交需要修改",
        body=(
            "您好：\n"
            "您在任务《{task_title}》的提交未通过审核，请在{revision_deadline_at}前"
            "修改后重新提交。\n审核意见：{review_comment}" + _EMAIL_SIGNOFF
        ),
    ),
    (
        NotificationEventType.REVISION_REQUIRED,
        NotificationChannel.IN_APP,
    ): TemplateText(
        title="提交需要修改",
        body=(
            "您在任务《{task_title}》的提交未通过审核，请在{revision_deadline_at}前"
            "修改后重新提交。审核意见：{review_comment}"
        ),
    ),
    (
        NotificationEventType.SUBMISSION_APPROVED,
        NotificationChannel.SMS,
    ): TemplateText(
        title="任务审核通过",
        body="您在任务《{task_title}》的提交已通过审核，获得{reward_points}积分。"
        + _SMS_SIGNATURE,
    ),
    (
        NotificationEventType.SUBMISSION_APPROVED,
        NotificationChannel.EMAIL,
    ): TemplateText(
        title="任务审核通过",
        body=(
            "您好：\n"
            "您在任务《{task_title}》的提交已通过审核，{reward_points}积分已入账。"
            + _EMAIL_SIGNOFF
        ),
    ),
    (
        NotificationEventType.SUBMISSION_APPROVED,
        NotificationChannel.IN_APP,
    ): TemplateText(
        title="任务审核通过",
        body="您在任务《{task_title}》的提交已通过审核，获得{reward_points}积分。",
    ),
    (
        NotificationEventType.SUBMISSION_VALIDATION_FAILED,
        NotificationChannel.SMS,
    ): TemplateText(
        title="提交校验未通过",
        body="您在任务《{task_title}》的提交未通过自动校验，请修正后重新提交。"
        + _SMS_SIGNATURE,
    ),
    (
        NotificationEventType.SUBMISSION_VALIDATION_FAILED,
        NotificationChannel.EMAIL,
    ): TemplateText(
        title="提交未通过自动校验",
        body=(
            "您好：\n"
            "您在任务《{task_title}》的提交未通过自动校验：{validation_summary}\n"
            "请修正问题后重新提交。" + _EMAIL_SIGNOFF
        ),
    ),
    (
        NotificationEventType.SUBMISSION_VALIDATION_FAILED,
        NotificationChannel.IN_APP,
    ): TemplateText(
        title="提交未通过自动校验",
        body=(
            "您在任务《{task_title}》的提交未通过自动校验：{validation_summary}。"
            "请修正后重新提交。"
        ),
    ),
    (
        NotificationEventType.REWARD_REDEMPTION_APPROVED,
        NotificationChannel.SMS,
    ): TemplateText(
        title="兑换成功",
        body="您兑换的「{item_name}」已通过审核，消耗{points_spent}积分，请留意领取通知。"
        + _SMS_SIGNATURE,
    ),
    (
        NotificationEventType.REWARD_REDEMPTION_APPROVED,
        NotificationChannel.EMAIL,
    ): TemplateText(
        title="兑换申请已通过",
        body=(
            "您好：\n"
            "您使用{points_spent}积分兑换的「{item_name}」已通过审核，"
            "请按站内通知的领取方式领取。" + _EMAIL_SIGNOFF
        ),
    ),
    (
        NotificationEventType.REWARD_REDEMPTION_APPROVED,
        NotificationChannel.IN_APP,
    ): TemplateText(
        title="兑换申请已通过",
        body="您兑换的「{item_name}」已通过审核，消耗{points_spent}积分。",
    ),
    (
        NotificationEventType.REWARD_REDEMPTION_REJECTED,
        NotificationChannel.SMS,
    ): TemplateText(
        title="兑换未通过",
        body="您对「{item_name}」的兑换未通过，{points_refunded}积分已退回。"
        + _SMS_SIGNATURE,
    ),
    (
        NotificationEventType.REWARD_REDEMPTION_REJECTED,
        NotificationChannel.EMAIL,
    ): TemplateText(
        title="兑换申请未通过",
        body=(
            "您好：\n"
            "您对「{item_name}」的兑换申请未通过，原因：{rejection_reason}。\n"
            "冻结的{points_refunded}积分已退回可用余额。" + _EMAIL_SIGNOFF
        ),
    ),
    (
        NotificationEventType.REWARD_REDEMPTION_REJECTED,
        NotificationChannel.IN_APP,
    ): TemplateText(
        title="兑换申请未通过",
        body=(
            "您对「{item_name}」的兑换申请未通过，原因：{rejection_reason}。"
            "{points_refunded}积分已退回。"
        ),
    ),
    (
        NotificationEventType.ACCOUNT_SECURITY,
        NotificationChannel.SMS,
    ): TemplateText(
        title="账号安全提醒",
        body="{event_time}，{event_summary}。如非本人操作，请尽快修改密码。"
        + _SMS_SIGNATURE,
    ),
    (
        NotificationEventType.ACCOUNT_SECURITY,
        NotificationChannel.EMAIL,
    ): TemplateText(
        title="账号安全提醒",
        body=(
            "您好：\n"
            "{event_time}，{event_summary}。\n"
            "如非本人操作，请立即修改密码并检查账号安全设置。" + _EMAIL_SIGNOFF
        ),
    ),
    (
        NotificationEventType.ACCOUNT_SECURITY,
        NotificationChannel.IN_APP,
    ): TemplateText(
        title="账号安全提醒",
        body="{event_time}，{event_summary}。如非本人操作，请尽快修改密码。",
    ),
}


def _substitute_placeholders(
    text: str,
    *,
    event_type: NotificationEventType,
    channel: NotificationChannel,
    allowed: frozenset[str],
    variables: Mapping[str, str],
) -> str:
    """Substitute ``{name}`` placeholders in `text` — the one scanner
    every render path shares (see the module docstring's safety
    argument). `allowed` names the namespace THIS call honors; a
    placeholder outside it is `InvalidTemplateError`, a name without a
    value is `MissingTemplateVariableError`, a non-str value a
    `TypeError`."""

    def _substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        if _VARIABLE_NAME_RE.match(name) is None or name not in allowed:
            raise InvalidTemplateError(event_type, channel, name, allowed)
        value = variables.get(name)
        if value is None:
            raise MissingTemplateVariableError(event_type, channel, name)
        if not isinstance(value, str):
            raise TypeError(
                f"notification template variable {name!r} must be str, "
                f"got {type(value).__name__}; format dates and numbers "
                "at the call site"
            )
        # Function replacement: `value` is inserted verbatim — no
        # backreference processing, no second scan.
        return value

    return _PLACEHOLDER_RE.sub(_substitute, text)


def render_template(
    event_type: NotificationEventType,
    channel: NotificationChannel,
    variables: Mapping[str, str],
    *,
    template: TemplateText | None = None,
) -> RenderedMessage:
    """Render the (event_type, channel) message from the seed template
    or a managed-row override (`template=`), substituting whitelisted
    `{name}` placeholders from `variables`.

    Raises `InvalidTemplateError` for a non-whitelisted placeholder in
    the template text, `MissingTemplateVariableError` for a whitelisted
    variable without a value, `TypeError` for a non-str value, and
    `ValueError` for an unknown event type or channel (programming
    errors). See the module docstring for the safety argument.
    """

    if event_type not in EVENT_VARIABLES:
        raise ValueError(
            f"unknown notification event type {event_type!r}; expected one of "
            f"{sorted(event.value for event in NotificationEventType)}"
        )
    if channel not in _ALL_CHANNELS:
        raise ValueError(
            f"unknown notification channel {channel!r}; expected one of "
            f"{sorted(channel.value for channel in NotificationChannel)}"
        )

    source = (
        template if template is not None else DEFAULT_TEMPLATES[(event_type, channel)]
    )
    allowed = EVENT_VARIABLES[event_type]
    return RenderedMessage(
        title=_substitute_placeholders(
            source.title,
            event_type=event_type,
            channel=channel,
            allowed=allowed,
            variables=variables,
        ),
        body=_substitute_placeholders(
            source.body,
            event_type=event_type,
            channel=channel,
            allowed=allowed,
            variables=variables,
        ),
    )


# --- dispatch-side render of managed SMS/EMAIL rows (PR #5 gfix C) ------------------
#
# The T1 schema persists no event payload: the per-event render
# variables exist only at record time, and dispatch holds the
# Notification snapshot (title/body). A managed SMS/EMAIL row therefore
# renders from exactly the variable set the SmsSender/EmailSender ports
# have always carried — {"title", "body"} — which is also what a real
# provider-side template registry would substitute from the same port
# call. Placeholders outside this namespace (e.g. an admin row carrying
# the record-time {task_title} grammar the W4 write gate still accepts)
# fail loudly at dispatch instead of shipping mangled copy; see
# render_snapshot_template.

SNAPSHOT_VARIABLES: frozenset[str] = frozenset({"title", "body"})


def render_snapshot_template(
    event_type: NotificationEventType,
    channel: NotificationChannel,
    template: TemplateText,
    *,
    title: str,
    body: str,
) -> RenderedMessage:
    """Render a managed SMS/EMAIL template row at dispatch time from
    the notification snapshot (`title`/`body` — the ports' frozen
    variable contract, `SNAPSHOT_VARIABLES`).

    The same scanner and failure classes as `render_template`; only the
    namespace differs, because the dispatch call site can supply only
    the snapshot pair. Raises `ValueError` for an unknown event type or
    channel, `InvalidTemplateError` for a placeholder outside
    `SNAPSHOT_VARIABLES`, `MissingTemplateVariableError` never in
    practice (both snapshot values are required `str`s here)."""

    if event_type not in EVENT_VARIABLES:
        raise ValueError(
            f"unknown notification event type {event_type!r}; expected one of "
            f"{sorted(event.value for event in NotificationEventType)}"
        )
    if channel not in _ALL_CHANNELS:
        raise ValueError(
            f"unknown notification channel {channel!r}; expected one of "
            f"{sorted(channel.value for channel in NotificationChannel)}"
        )
    variables = {"title": title, "body": body}
    return RenderedMessage(
        title=_substitute_placeholders(
            template.title,
            event_type=event_type,
            channel=channel,
            allowed=SNAPSHOT_VARIABLES,
            variables=variables,
        ),
        body=_substitute_placeholders(
            template.body,
            event_type=event_type,
            channel=channel,
            allowed=SNAPSHOT_VARIABLES,
            variables=variables,
        ),
    )


# --- write-time validation for Admin template edits (Plan 08 T5) --------------------
#
# V1 templates are PURE {name} placeholder substitution (the module
# docstring's safety argument). These sequences are therefore never
# legitimate template text: Jinja expression/block delimiters would NOT
# be interpreted by this renderer — the inner {name} would substitute
# and the surrounding braces would ship LITERALLY ({{task_title}}
# renders as "{value}") — so accepting them silently changes what the
# admin meant, and ${...} additionally reads as an injection attempt to
# every downstream system that interprets it. Rejecting them at write
# time fails the edit with a nameable reason instead of shipping mangled
# copy (plan 08 step 1's "unsafe executable template expressions are
# rejected").

_UNSAFE_TEMPLATE_MARKERS: tuple[str, ...] = ("{{", "}}", "{%", "${")


def find_unsafe_template_marker(text: str) -> str | None:
    """The first unsafe marker (``{{`` / ``}}`` / ``{%`` / ``${``) in
    ``text``, or ``None``. A conservative lexical scan by design: it
    errs on the side of rejecting (a stray ``}}`` after a placeholder
    is a typo worth naming), because the renderer grants these
    sequences no meaning and no legitimate V1 template contains them."""
    for marker in _UNSAFE_TEMPLATE_MARKERS:
        if marker in text:
            return marker
    return None


def validate_admin_template(
    event_type: NotificationEventType,
    channel: NotificationChannel,
    *,
    title: str,
    body: str,
) -> None:
    """Write-time gate for an Admin-edited template pair (Plan 08 T5):
    the text must be exactly what the renderer could honor — no
    Jinja/injection-style markers (``UnsafeTemplateMarkupError``), and
    every braced group exactly one whitelisted variable name for the
    event type (``InvalidTemplateError``, the render-time grammar
    applied BEFORE the row can persist). A template that passes here
    can still fail at render with ``MissingTemplateVariableError`` —
    that is a dispatch/payload bug, not a template defect, and no
    write-time check can predict it.

    ``channel`` participates in the error context only (the variable
    whitelist is per event type, shared across channels). Raises
    ``ValueError`` for an unknown event type or channel (programming
    errors), mirroring ``render_template``."""
    if event_type not in EVENT_VARIABLES:
        raise ValueError(
            f"unknown notification event type {event_type!r}; expected one of "
            f"{sorted(event.value for event in NotificationEventType)}"
        )
    if channel not in _ALL_CHANNELS:
        raise ValueError(
            f"unknown notification channel {channel!r}; expected one of "
            f"{sorted(channel.value for channel in NotificationChannel)}"
        )
    for field, text in (("title", title), ("template_body", body)):
        marker = find_unsafe_template_marker(text)
        if marker is not None:
            raise UnsafeTemplateMarkupError(event_type, channel, field, marker)
    allowed = EVENT_VARIABLES[event_type]
    for _field, text in (("title", title), ("template_body", body)):
        for match in _PLACEHOLDER_RE.finditer(text):
            name = match.group(1)
            if _VARIABLE_NAME_RE.match(name) is None or name not in allowed:
                raise InvalidTemplateError(event_type, channel, name, allowed)


def _validate_seed_templates() -> None:
    """Import-time drift check: every seed placeholder must be a
    whitelisted variable name for its event type. Keeps
    `EVENT_VARIABLES` and `DEFAULT_TEMPLATES` from silently diverging;
    violations mean this module is broken, not any caller."""

    for (event_type, channel), seed in DEFAULT_TEMPLATES.items():
        for text in (seed.title, seed.body):
            for match in _PLACEHOLDER_RE.finditer(text):
                name = match.group(1)
                if _VARIABLE_NAME_RE.match(name) is None or (
                    name not in EVENT_VARIABLES[event_type]
                ):
                    raise RuntimeError(
                        f"seed template {event_type}/{channel} contains "
                        f"non-whitelisted placeholder {name!r}"
                    )


_validate_seed_templates()
