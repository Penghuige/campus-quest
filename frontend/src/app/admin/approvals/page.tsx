import type { Metadata } from "next";

import { ApprovalsAdmin } from "@/features/admin/ApprovalsAdmin";

export const metadata: Metadata = {
  title: "注册审批 · CampusQuest 管理后台",
  description: "待审核学生队列：通过解锁全部功能，拒绝封禁账号",
};

/**
 * The registration-approvals page (auth-extension-II spec §5): the
 * PENDING_APPROVAL queue replaces the retired whitelist entry in the
 * admin nav — the register gate moved from a pre-checked list to a
 * post-registration review.
 */
export default function AdminApprovalsPage() {
  return (
    <>
      <div className="page-head">
        <h1 className="page-title">注册审批</h1>
        <p className="page-subtitle">
          审核新注册学生：通过后立即解锁全部功能；拒绝将封禁账号（可附理由）
        </p>
      </div>
      <ApprovalsAdmin />
    </>
  );
}
