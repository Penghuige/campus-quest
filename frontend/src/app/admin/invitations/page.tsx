import type { Metadata } from "next";

import { StaffInvitations } from "@/features/admin/StaffInvitations";

export const metadata: Metadata = {
  title: "员工邀请 · CampusQuest 管理后台",
  description: "向教师或管理员发放一次性邀请链接（48 小时有效，仅显示一次）",
};

/**
 * Staff-invitation issuance page (spec §5.8 step 1; owner request
 * 2026-10-14): productizing what used to be an ssh-and-curl script on
 * the production host. Backend surface unchanged — POST
 * /staff/invitations (Admin-only, rate-limited 20/hour per admin).
 */
export default function AdminInvitationsPage() {
  return (
    <>
      <div className="page-head">
        <h1 className="page-title">员工邀请</h1>
        <p className="page-subtitle">
          发放一次性邀请链接，邀请教师或管理员加入；链接 48 小时内有效，创建后只显示一次
        </p>
      </div>
      <StaffInvitations />
    </>
  );
}
