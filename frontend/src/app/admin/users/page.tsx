import type { Metadata } from "next";

import { AdminUserAccounts } from "@/features/admin/AdminUserAccounts";

export const metadata: Metadata = {
  title: "用户与账户 · CampusQuest 管理后台",
  description: "账号目录与状态管理（停用 / 封禁 / 恢复）",
};

/**
 * Admin users page (spec §5.7): the account directory island with
 * role/status filters and the three audited status verbs.
 */
export default function AdminUsersPage() {
  return (
    <>
      <div className="page-head">
        <h1 className="page-title">用户与账户</h1>
        <p className="page-subtitle">
          账号目录与状态管理；停用 / 封禁 / 恢复均需填写原因并记入审计日志
        </p>
      </div>
      <AdminUserAccounts />
    </>
  );
}
