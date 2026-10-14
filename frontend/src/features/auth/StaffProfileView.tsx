"use client";
/**
 * Defect #17 (QA 2026-10-03): the teacher workspace's own profile
 * view — the account facts /me already carries (spec §40: the owner's
 * own account view), rendered as definition rows under the teacher
 * shell.
 *
 * Field scope: 昵称/账号/角色/账号状态 from the original DTO;
 * 注册日期 carried by MePublic.created_at since PR #46 — formatted
 * date-only in the business timezone, the growth view's honor-date
 * shape.
 *
 * V1.1 (plan Task 10 ruling): the nickname edit row. The rename nudge
 * points staff here, and PATCH /me/nickname accepts any active role —
 * until this row the endpoint had no staff UI, which would have made
 * the nudge a dead end. Reuses the shared validation + API modules the
 * student AccountSettings editor rides (no second nickname rule).
 */
import { useState, type FormEvent } from "react";

import { AuthField } from "./AuthField";
import { DevicePanel } from "./DevicePanel";
import { FormErrorSummary } from "./FormErrorSummary";
import { SubmitButton } from "./SubmitButton";
import { updateNickname } from "./api";
import { describeAuthError, NETWORK_ERROR_TEXT, type AuthErrorView } from "./errors";
import { useSession } from "./session";
import {
  countGraphemes,
  NICKNAME_MAX_GRAPHEME_CLUSTERS,
  validateNickname,
} from "./validation";
import { isApiError } from "@/lib/errors";
import { BUSINESS_TIME_CONFIG } from "@/lib/time";

/** Account status text — the admin surfaces' established mapping. */
const STATUS_LABELS: Record<string, string> = {
  ACTIVE: "正常",
  SUSPENDED: "已停用",
};

/** Date-only, year-inclusive, business timezone (formatHonorDate's shape). */
function formatRegistrationDate(iso: string): string {
  return new Intl.DateTimeFormat(BUSINESS_TIME_CONFIG.locale, {
    timeZone: BUSINESS_TIME_CONFIG.timeZone,
    year: "numeric",
    month: "long",
    day: "numeric",
  }).format(new Date(Date.parse(iso)));
}

export function StaffProfileView() {
  const { state, refresh } = useSession();
  if (state.status !== "authenticated") {
    return null; // The shell gates the whole workspace on the session.
  }
  const me = state.me;

  return (
    <section className="section" aria-label="个人信息">
      <div className="section-head">
        <h2 className="section-title">个人信息</h2>
      </div>
      <p className="field-hint">当前登录的员工账号信息。</p>
      <dl className="task-facts">
        <div className="fact-row">
          <dt className="fact-label">昵称</dt>
          <dd className="fact-value">{me.nickname}</dd>
        </div>
        <div className="fact-row">
          <dt className="fact-label">账号</dt>
          <dd className="fact-value">{me.username}</dd>
        </div>
        <div className="fact-row">
          <dt className="fact-label">角色</dt>
          <dd className="fact-value">{me.role === "ADMIN" ? "管理员" : "教师"}</dd>
        </div>
        <div className="fact-row">
          <dt className="fact-label">账号状态</dt>
          <dd className="fact-value">
            {STATUS_LABELS[me.status] ?? me.status}
          </dd>
        </div>
        {/* PR #46's contract addition: the registration instant,
            display-only. */}
        <div className="fact-row">
          <dt className="fact-label">注册日期</dt>
          <dd className="fact-value" suppressHydrationWarning>
            {formatRegistrationDate(me.created_at)}
          </dd>
        </div>
      </dl>
      <NicknameEditRow me={me} onUpdated={refresh} />
      <DevicePanel />
    </section>
  );
}

/** The one editable staff fact (V1.1 Task 10): nickname, the same
 * shared rules the student editor rides. */
function NicknameEditRow({
  me,
  onUpdated,
}: {
  me: { nickname: string };
  onUpdated: () => void;
}) {
  const [value, setValue] = useState(me.nickname);
  const [fieldError, setFieldError] = useState<string | undefined>(undefined);
  const [summary, setSummary] = useState<AuthErrorView | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [savedNote, setSavedNote] = useState(false);

  const count = countGraphemes(value);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting) {
      return;
    }
    setSavedNote(false);
    const error = validateNickname(value) ?? undefined;
    setFieldError(error);
    setSummary(null);
    if (error !== undefined) {
      return;
    }
    setSubmitting(true);
    try {
      await updateNickname(value.trim());
      onUpdated();
      setSavedNote(true);
    } catch (cause) {
      setSummary(
        isApiError(cause)
          ? describeAuthError(cause)
          : { summary: NETWORK_ERROR_TEXT, fieldErrors: {}, requestId: null },
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form className="panel form" onSubmit={onSubmit} noValidate>
      <FormErrorSummary view={summary} />
      <AuthField
        name="staff-nickname"
        label="修改昵称"
        counter={`${count}/${NICKNAME_MAX_GRAPHEME_CLUSTERS}`}
        counterOver={count > NICKNAME_MAX_GRAPHEME_CLUSTERS}
        hint="展示给同事和学生的名称；按显示字符计数，emoji 算 1 个。"
        error={fieldError}
        inputProps={{
          value,
          onChange: (event) => {
            setValue(event.target.value);
            if (fieldError !== undefined) {
              setFieldError(undefined);
            }
          },
          type: "text",
          autoComplete: "nickname",
          disabled: submitting,
        }}
      />
      {savedNote ? (
        <p className="alert alert-success" role="status">
          昵称已更新。
        </p>
      ) : null}
      <SubmitButton loading={submitting}>保存昵称</SubmitButton>
    </form>
  );
}
