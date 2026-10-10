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
 */
import { BUSINESS_TIME_CONFIG } from "@/lib/time";

import { useSession } from "./session";

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
  const { state } = useSession();
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
    </section>
  );
}
