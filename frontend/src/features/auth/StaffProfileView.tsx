"use client";
/**
 * Defect #17 (QA 2026-10-03): the teacher workspace's own profile
 * view — the account facts /me already carries (spec §40: the owner's
 * own account view), rendered as definition rows under the teacher
 * shell.
 *
 * Field scope ruling (reviewer, 2026-10-09): ONLY fields the DTO
 * already exports — 昵称/账号/角色/账号状态. 注册日期 waits on the
 * backend contract (MePublic carries no created_at; the gap is filed
 * and lands as a DTO addition), and no field is invented client-side.
 */
import { useSession } from "./session";

/** Account status text — the admin surfaces' established mapping. */
const STATUS_LABELS: Record<string, string> = {
  ACTIVE: "正常",
  SUSPENDED: "已停用",
};

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
      </dl>
    </section>
  );
}
